"""Wire codecs and real in-process gRPC transport, without generated stubs or extra dependencies."""
import asyncio
import json
import struct

import grpc
import pytest
from pydantic import SecretStr

from nabd.salesforce.pubsub import (
    AvroReader, PubSubClient, PubSubDecodeError, ReplayRejected, FetchRequest, CUSTOM,
    GET_TOPIC, GET_SCHEMA, SUBSCRIBE, SERVICE, decode_message, decode_fetch_response,
    _varint, _bytes_field as b, _varint_field as v,
)
from nabd.salesforce.rest import AccessToken


def avro_long(n):
    return _varint((n << 1) ^ (n >> 63))


def string(s):
    raw = s.encode()
    return avro_long(len(raw)) + raw


def test_protobuf_fields_unknowns_repeated_and_truncation():
    raw = b(1, b'first') + b(1, b'last') + v(2, 300) + bytes([29]) + struct.pack('<I', 42)
    assert decode_message(raw) == {1: [b'first', b'last'], 2: [300], 3: [42]}
    request = decode_message(FetchRequest('/event/Test__e', CUSTOM, b'opaque', 2).encode())
    assert request == {1: [b'/event/Test__e'], 2: [CUSTOM], 3: [b'opaque'], 4: [2]}
    assert decode_fetch_response(v(4, -1)).pending_num_requested == -1
    for malformed in [b'\x80', b'\x0a\x04a', b'\x09\x00', b'\x00', b'\x0f', b'\x80'*11]:
        with pytest.raises(PubSubDecodeError):
            decode_message(malformed)


def test_avro_nested_unions_named_enums_arrays_maps_and_invalid_data():
    schema = {'type':'record', 'name':'Event', 'fields': [
        {'name':'id','type':['null','string']}, {'name':'at','type':{'type':'long','logicalType':'timestamp-millis'}},
        {'name':'flags','type':{'type':'array','items':'boolean'}},
        {'name':'labels','type':{'type':'map','values':'string'}},
        {'name':'kind','type':{'type':'enum','name':'Kind','symbols':['case','comment']}},
        {'name':'again','type':'Kind'}]}
    raw = avro_long(1) + string('500') + avro_long(-42) + avro_long(-2) + avro_long(2) + b'\1\0' + avro_long(0)
    raw += avro_long(1) + string('a') + string('b') + avro_long(0) + avro_long(1) + avro_long(0)
    assert AvroReader(schema).read(raw) == {'id':'500','at':-42,'flags':[True,False], 'labels':{'a':'b'},
                                         'kind':'comment','again':'case'}
    for schema, raw in [('boolean', b'\2'), ({'type':'fixed','name':'F','size':4}, b'ab'),
                        ({'type':'enum','name':'E','symbols':['one']}, avro_long(-1)),
                        (['null','string'], avro_long(3)), ('string', avro_long(10)+b'a'), ('long', b''),
                        ('null', b'extra')]:
        with pytest.raises(PubSubDecodeError):
            AvroReader(json.dumps(schema) if isinstance(schema, str) else schema).read(raw)


def test_client_auth_refresh_flow_control_schema_cache_replay_and_keepalive():
    async def run():
        schema = {'type':'record','name':'Event','fields':[{'name':'Case_Id__c','type':'string'}]}
        requests, schemas, auth_calls = [], [], []
        class Auth:
            async def token(self, refresh=False):
                auth_calls.append(refresh)
                return AccessToken(SecretStr('fresh' if refresh else 'old'), 'https://test.my.salesforce.com', '00Dorg')
        async def topic(raw, context):
            md = dict(context.invocation_metadata())
            if md['accesstoken'] == 'old':
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, 'never logged')
            assert md['tenantid'] == '00Dorg'
            return b(1, b'/event/Test__e') + v(4, 1) + b(5, b's1')
        async def get_schema(raw, context):
            schemas.append(decode_message(raw))
            return b(1, json.dumps(schema).encode()) + b(2, b's1')
        async def subscribe(incoming, context):
            first = decode_message(await anext(incoming))
            requests.append(first)
            if first.get(3) == [b'expired']:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, 'expired')
            producer = b(1,b'event-1') + b(2,b's1') + b(3,string('500000000000001AAA'))
            consumer = b(1,producer) + b(2,b'cursor-1')
            yield b(1,consumer) + b(2,b'cursor-1') + v(4,0)
            requests.append(decode_message(await anext(incoming)))
            yield b(2,b'keepalive') + v(4,1)
            await asyncio.Event().wait()
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(SERVICE[1:], {
            'GetTopic': grpc.unary_unary_rpc_method_handler(topic),
            'GetSchema': grpc.unary_unary_rpc_method_handler(get_schema),
            'Subscribe': grpc.stream_stream_rpc_method_handler(subscribe),
        }),))
        port = server.add_insecure_port('127.0.0.1:0')
        await server.start()
        try:
            client = PubSubClient(Auth(), channel=lambda: grpc.aio.insecure_channel(f'127.0.0.1:{port}'))
            info = await client.topic('/event/Test__e')
            assert info.can_subscribe and not info.can_publish and auth_calls == [False, True]
            stream = client.subscribe('/event/Test__e', b'stored', batch_size=1)
            first = await anext(stream)
            assert first.events[0].fields['Case_Id__c'] == '500000000000001AAA'
            second = await anext(stream)
            assert second.events == () and second.latest_replay_id == b'keepalive'
            await stream.aclose()
            assert requests[0][2] == [CUSTOM] and requests[0][3] == [b'stored']
            assert requests[1] == {4:[1]}
            assert len(schemas) == 1
            await client.reader('s1')
            assert len(schemas) == 1
            stream = client.subscribe('/event/Test__e', b'expired')
            with pytest.raises(ReplayRejected):
                await anext(stream)
        finally:
            await server.stop(None)
    asyncio.run(run())
