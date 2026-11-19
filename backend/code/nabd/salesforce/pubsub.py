"""Salesforce Pub/Sub API client for the NABD case change event (Phase 5).

The Pub/Sub API is gRPC (``eventbus.v1.PubSub`` at api.pubsub.salesforce.com).
NABD uses three of its calls: ``GetTopic`` (may this user subscribe?),
``GetSchema`` (the event's Avro schema) and the bidirectional ``Subscribe``
stream with flow control and replay IDs.

The project depends on ``grpcio`` only (owner decision, Phase 5). The handful
of protobuf messages these calls exchange are encoded and decoded here, and
event payloads are read with a small Avro binary decoder driven by the schema
Salesforce returns. Both cover exactly the wire features these messages and
platform event schemas use; anything else raises ``PubSubDecodeError``.

Authentication reuses the integration user's OAuth token (client
credentials): gRPC metadata carries the access token, instance URL and org
ID. Tokens never appear in errors or logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import grpc

from nabd.salesforce.rest import AccessToken, SalesforceAuthError, SalesforceError, SalesforceUnavailable

logger = logging.getLogger(__name__)

PUBSUB_ENDPOINT = "api.pubsub.salesforce.com:7443"
SERVICE = "/eventbus.v1.PubSub"
SUBSCRIBE = f"{SERVICE}/Subscribe"
GET_SCHEMA = f"{SERVICE}/GetSchema"
GET_TOPIC = f"{SERVICE}/GetTopic"

# ReplayPreset enum of the Pub/Sub API.
LATEST, EARLIEST, CUSTOM = 0, 1, 2


class PubSubDecodeError(SalesforceError):
    """A Pub/Sub message or event payload did not have the expected shape."""


class ReplayRejected(SalesforceError):
    """Salesforce refused the stored replay ID (too old or corrupted); restart at the latest event."""


# --- Protobuf wire format (proto3), limited to these messages ------------------------------


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1  # negative int32/int64 are ten-byte varints
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise PubSubDecodeError("truncated or oversized varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _key(number: int, wire: int) -> bytes:
    return _varint((number << 3) | wire)


def _bytes_field(number: int, value: bytes) -> bytes:
    return _key(number, 2) + _varint(len(value)) + value


def _varint_field(number: int, value: int) -> bytes:
    return _key(number, 0) + _varint(value)


def decode_message(data: bytes) -> dict[int, list[Any]]:
    """Field number -> values (ints for varint/fixed fields, bytes for length-delimited ones)."""
    fields: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise PubSubDecodeError("field number 0")
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 1:
            if pos + 8 > len(data):
                raise PubSubDecodeError("truncated fixed64")
            value, pos = int.from_bytes(data[pos:pos + 8], "little"), pos + 8
        elif wire == 2:
            size, pos = _read_varint(data, pos)
            if pos + size > len(data):
                raise PubSubDecodeError("truncated length-delimited field")
            value, pos = data[pos:pos + size], pos + size
        elif wire == 5:
            if pos + 4 > len(data):
                raise PubSubDecodeError("truncated fixed32")
            value, pos = int.from_bytes(data[pos:pos + 4], "little"), pos + 4
        else:
            raise PubSubDecodeError(f"unsupported wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _one(fields: dict[int, list[Any]], number: int, default: Any) -> Any:
    values = fields.get(number)
    return values[-1] if values else default  # proto3: the last value of a repeated scalar wins


def _text(fields: dict[int, list[Any]], number: int) -> str:
    value = _one(fields, number, b"")
    if not isinstance(value, bytes):
        raise PubSubDecodeError(f"field {number} is not a string")
    return value.decode("utf-8")


@dataclass(frozen=True)
class FetchRequest:
    """``eventbus.v1.FetchRequest``. Only the first request of a stream names the topic and replay."""

    topic_name: str = ""
    replay_preset: int = LATEST
    replay_id: bytes = b""
    num_requested: int = 0

    def encode(self) -> bytes:
        out = b""
        if self.topic_name:
            out += _bytes_field(1, self.topic_name.encode())
        if self.replay_preset:
            out += _varint_field(2, self.replay_preset)
        if self.replay_id:
            out += _bytes_field(3, self.replay_id)
        if self.num_requested:
            out += _varint_field(4, self.num_requested)
        return out


def encode_schema_request(schema_id: str) -> bytes:
    return _bytes_field(1, schema_id.encode())


def encode_topic_request(topic_name: str) -> bytes:
    return _bytes_field(1, topic_name.encode())


@dataclass(frozen=True)
class ReceivedEvent:
    """One ``ConsumerEvent``: its replay ID and the still-encoded ``ProducerEvent``."""

    replay_id: bytes
    event_id: str
    schema_id: str
    payload: bytes


@dataclass(frozen=True)
class FetchResponse:
    events: tuple[ReceivedEvent, ...]
    latest_replay_id: bytes
    pending_num_requested: int


def decode_fetch_response(data: bytes) -> FetchResponse:
    fields = decode_message(data)
    events = []
    for raw in fields.get(1, []):
        consumer = decode_message(raw)
        producer = decode_message(_one(consumer, 1, b""))
        events.append(ReceivedEvent(
            replay_id=_one(consumer, 2, b""), event_id=_text(producer, 1), schema_id=_text(producer, 2),
            payload=_one(producer, 3, b"")))
    pending = _one(fields, 4, 0)
    if pending >= 1 << 31:
        pending -= 1 << 64  # int32 encoded as a negative varint
    return FetchResponse(tuple(events), _one(fields, 2, b""), pending)


def decode_schema_info(data: bytes) -> tuple[str, str]:
    """(schema JSON, schema ID) of ``eventbus.v1.SchemaInfo``."""
    fields = decode_message(data)
    return _text(fields, 1), _text(fields, 2)


@dataclass(frozen=True)
class TopicInfo:
    topic_name: str
    can_publish: bool
    can_subscribe: bool
    schema_id: str


def decode_topic_info(data: bytes) -> TopicInfo:
    fields = decode_message(data)
    return TopicInfo(_text(fields, 1), bool(_one(fields, 3, 0)), bool(_one(fields, 4, 0)), _text(fields, 5))


# --- Avro binary decoding of one record ---------------------------------------------------


class AvroReader:
    """Decodes Avro binary data written with ``schema`` (the writer schema from GetSchema)."""

    PRIMITIVES = {"null", "boolean", "int", "long", "float", "double", "bytes", "string"}

    def __init__(self, schema: str | dict[str, Any]):
        self.schema = json.loads(schema) if isinstance(schema, str) else schema
        self.named: dict[str, Any] = {}
        self._register(self.schema, None)

    def _register(self, schema: Any, namespace: str | None) -> None:
        if isinstance(schema, list):
            for branch in schema:
                self._register(branch, namespace)
        elif isinstance(schema, dict):
            kind = schema.get("type")
            if kind in ("record", "enum", "fixed"):
                space = schema.get("namespace", namespace)
                name = schema["name"]
                self.named[name] = schema
                if space and "." not in name:
                    self.named[f"{space}.{name}"] = schema
                for item in schema.get("fields", []):
                    self._register(item["type"], space)
            elif kind == "array":
                self._register(schema["items"], namespace)
            elif kind == "map":
                self._register(schema["values"], namespace)
            elif isinstance(kind, (dict, list)):
                self._register(kind, namespace)

    def read(self, data: bytes) -> Any:
        value, pos = self._read(self.schema, data, 0)
        if pos != len(data):
            raise PubSubDecodeError("trailing bytes after the Avro record")
        return value

    def _long(self, data: bytes, pos: int) -> tuple[int, int]:
        raw, pos = _read_varint(data, pos)
        return (raw >> 1) ^ -(raw & 1), pos

    def _sized(self, data: bytes, pos: int) -> tuple[bytes, int]:
        size, pos = self._long(data, pos)
        if size < 0 or pos + size > len(data):
            raise PubSubDecodeError("invalid Avro length")
        return data[pos:pos + size], pos + size

    def _read(self, schema: Any, data: bytes, pos: int) -> tuple[Any, int]:
        if isinstance(schema, list):  # union: branch index, then the value
            index, pos = self._long(data, pos)
            if not 0 <= index < len(schema):
                raise PubSubDecodeError("invalid Avro union branch")
            return self._read(schema[index], data, pos)
        if isinstance(schema, str):
            if schema not in self.PRIMITIVES:
                if schema not in self.named:
                    raise PubSubDecodeError(f"unknown Avro type {schema}")
                return self._read(self.named[schema], data, pos)
            schema = {"type": schema}
        kind = schema["type"]
        if isinstance(kind, (list, dict)):
            return self._read(kind, data, pos)
        if kind == "null":
            return None, pos
        if kind == "boolean":
            if pos >= len(data):
                raise PubSubDecodeError("truncated Avro boolean")
            if data[pos] not in (0, 1):
                raise PubSubDecodeError("invalid Avro boolean")
            return data[pos] == 1, pos + 1
        if kind in ("int", "long"):
            return self._long(data, pos)
        if kind in ("float", "double"):
            size, fmt = (4, "<f") if kind == "float" else (8, "<d")
            if pos + size > len(data):
                raise PubSubDecodeError(f"truncated Avro {kind}")
            return struct.unpack(fmt, data[pos:pos + size])[0], pos + size
        if kind == "bytes":
            return self._sized(data, pos)
        if kind == "string":
            raw, pos = self._sized(data, pos)
            return raw.decode("utf-8"), pos
        if kind == "record":
            record = {}
            for item in schema["fields"]:
                record[item["name"]], pos = self._read(item["type"], data, pos)
            return record, pos
        if kind == "enum":
            index, pos = self._long(data, pos)
            if not 0 <= index < len(schema["symbols"]):
                raise PubSubDecodeError("invalid Avro enum index")
            return schema["symbols"][index], pos
        if kind == "fixed":
            size = schema["size"]
            if size < 0 or pos + size > len(data):
                raise PubSubDecodeError("truncated Avro fixed")
            return data[pos:pos + size], pos + size
        if kind in ("array", "map"):
            items: list[Any] = []
            entries: dict[str, Any] = {}
            while True:
                count, pos = self._long(data, pos)
                if count == 0:
                    break
                if count < 0:
                    count = -count
                    _, pos = self._long(data, pos)  # block size in bytes, unused
                for _ in range(count):
                    if kind == "array":
                        value, pos = self._read(schema["items"], data, pos)
                        items.append(value)
                    else:
                        key, pos = self._sized(data, pos)
                        entries[key.decode("utf-8")], pos = self._read(schema["values"], data, pos)
            return (items if kind == "array" else entries), pos
        raise PubSubDecodeError(f"unsupported Avro type {kind}")


# --- Client -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedEvent:
    replay_id: bytes
    event_id: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class EventBatch:
    """What one ``FetchResponse`` delivered. Keepalives have no events but a newer replay ID."""

    events: tuple[DecodedEvent, ...]
    latest_replay_id: bytes


ChannelFactory = Callable[[], grpc.aio.Channel]


def secure_channel(endpoint: str = PUBSUB_ENDPOINT) -> ChannelFactory:
    def make() -> grpc.aio.Channel:
        # Keepalive pings detect half-open connections between Salesforce's own keepalive messages.
        return grpc.aio.secure_channel(endpoint, grpc.ssl_channel_credentials(), options=[
            ("grpc.keepalive_time_ms", 60_000), ("grpc.keepalive_timeout_ms", 20_000)])
    return make


def _metadata(token: AccessToken) -> tuple[tuple[str, str], ...]:
    if not token.org_id:
        raise SalesforceAuthError("The Salesforce token does not name its org.")
    return (("accesstoken", token.value.get_secret_value()), ("instanceurl", token.instance_url),
            ("tenantid", token.org_id))


def _translate(error: grpc.aio.AioRpcError, *, replay: bool) -> SalesforceError:
    code = error.code()
    if code == grpc.StatusCode.UNAUTHENTICATED:
        return SalesforceAuthError("Pub/Sub API rejected the session.")
    if code == grpc.StatusCode.PERMISSION_DENIED:
        return SalesforceAuthError("The integration user may not subscribe to this topic.")
    if replay and code == grpc.StatusCode.INVALID_ARGUMENT:
        return ReplayRejected("Salesforce refused the stored replay ID.")
    return SalesforceUnavailable(f"Pub/Sub API call failed ({code.name}).")


@dataclass
class PubSubClient:
    auth: Any  # TokenSource returning AccessToken with org_id
    channel: ChannelFactory = field(default_factory=secure_channel)
    timeout_seconds: float = 20.0
    _schemas: dict[str, AvroReader] = field(default_factory=dict, init=False, repr=False)

    async def _unary(self, method: str, request: bytes, *, refresh: bool = False) -> bytes:
        token = await self.auth.token(refresh=refresh)
        async with self.channel() as channel:
            call = channel.unary_unary(method)
            try:
                return await call(request, metadata=_metadata(token), timeout=self.timeout_seconds)
            except grpc.aio.AioRpcError as error:
                if error.code() == grpc.StatusCode.UNAUTHENTICATED and not refresh:
                    return await self._unary(method, request, refresh=True)
                raise _translate(error, replay=False) from None

    async def topic(self, topic_name: str) -> TopicInfo:
        return decode_topic_info(await self._unary(GET_TOPIC, encode_topic_request(topic_name)))

    async def reader(self, schema_id: str) -> AvroReader:
        if schema_id not in self._schemas:
            schema_json, _ = decode_schema_info(await self._unary(GET_SCHEMA, encode_schema_request(schema_id)))
            self._schemas[schema_id] = AvroReader(schema_json)
        return self._schemas[schema_id]

    async def subscribe(self, topic_name: str, replay_id: bytes | None, *, batch_size: int = 100,
                        refresh: bool = False) -> AsyncIterator[EventBatch]:
        """Stream event batches, resuming after ``replay_id`` (or at the latest event).

        Requests ``batch_size`` more events whenever the previous request is
        used up. Ends only by raising: ``ReplayRejected`` for an unusable replay
        ID, ``SalesforceAuthError`` or ``SalesforceUnavailable`` otherwise.
        """
        token = await self.auth.token(refresh=refresh)
        requests: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def outgoing() -> AsyncIterator[bytes]:
            while (item := await requests.get()) is not None:
                yield item

        first = FetchRequest(topic_name, CUSTOM if replay_id else LATEST, replay_id or b"", batch_size)
        await requests.put(first.encode())
        async with self.channel() as channel:
            stream = channel.stream_stream(SUBSCRIBE)(outgoing(), metadata=_metadata(token))
            try:
                async for raw in stream:
                    response = decode_fetch_response(raw)
                    events = []
                    for item in response.events:
                        reader = await self.reader(item.schema_id)
                        events.append(DecodedEvent(item.replay_id, item.event_id, reader.read(item.payload)))
                    yield EventBatch(tuple(events), response.latest_replay_id or
                                     (events[-1].replay_id if events else b""))
                    if response.pending_num_requested <= 0:
                        await requests.put(FetchRequest(num_requested=batch_size).encode())
            except grpc.aio.AioRpcError as error:
                raise _translate(error, replay=replay_id is not None) from None
            finally:
                await requests.put(None)
                stream.cancel()
        raise SalesforceUnavailable("Pub/Sub API closed the subscription.")
