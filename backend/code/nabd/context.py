"""Signed customer context: how the API tells the read MCP server whom a run serves.

The model never supplies the customer. For each run the API mints a short-lived
token, HMAC-SHA256-signed with a secret shared only by the API and the MCP
server, naming the customer, conversation, run, audience and scopes. The MCP
server verifies it on every request and scopes every Salesforce read by it.

Format: ``v1.<base64url(JSON claims)>.<base64url(HMAC of "v1.<claims>")>``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from pydantic import SecretStr

from nabd.salesforce.soql import CUSTOMER_ID

READ_AUDIENCE = "nabd-read-mcp"
READ_SCOPE = "nabd.read"
ISSUER = "nabd-api"
_VERSION = "v1"
_MAX_TOKEN_LENGTH = 2048


class InvalidContext(Exception):
    """The token is missing, malformed, tampered with, expired or for another audience."""


@dataclass(frozen=True)
class CustomerContext:
    customer_id: str
    conversation_id: str
    run_id: str
    scopes: tuple[str, ...]
    audience: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: SecretStr, signed: str) -> str:
    return _b64(hmac.new(secret.get_secret_value().encode(), signed.encode("ascii"), hashlib.sha256).digest())


def mint(
    secret: SecretStr,
    *,
    customer_id: str,
    conversation_id: str,
    run_id: str,
    ttl_seconds: float,
    scopes: tuple[str, ...] = (READ_SCOPE,),
    audience: str = READ_AUDIENCE,
    now: float | None = None,
) -> str:
    if not CUSTOMER_ID.fullmatch(customer_id):
        raise ValueError("customer ID has an invalid format")
    issued = int(now if now is not None else time.time())
    claims = {
        "iss": ISSUER, "aud": audience, "sub": customer_id, "conv": conversation_id, "run": run_id,
        "scope": " ".join(scopes), "iat": issued, "exp": issued + max(1, int(ttl_seconds)),
    }
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signed = f"{_VERSION}.{body}"
    return f"{signed}.{_signature(secret, signed)}"


def verify(secret: SecretStr, token: str, *, audience: str = READ_AUDIENCE, now: float | None = None) -> CustomerContext:
    if not isinstance(token, str) or len(token) > _MAX_TOKEN_LENGTH:
        raise InvalidContext("malformed context token")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _VERSION:
        raise InvalidContext("malformed context token")
    if not hmac.compare_digest(_signature(secret, f"{parts[0]}.{parts[1]}"), parts[2]):
        raise InvalidContext("context token signature does not match")
    try:
        claims = json.loads(_unb64(parts[1]))
        customer_id, conversation_id, run_id = str(claims["sub"]), str(claims["conv"]), str(claims["run"])
        expires_at, scopes = int(claims["exp"]), tuple(str(claims["scope"]).split())
        token_audience, issuer = str(claims["aud"]), str(claims["iss"])
    except (ValueError, KeyError, TypeError):
        raise InvalidContext("malformed context token") from None
    if issuer != ISSUER or token_audience != audience:
        raise InvalidContext("context token is for another audience")
    if expires_at <= (now if now is not None else time.time()):
        raise InvalidContext("context token has expired")
    if not CUSTOMER_ID.fullmatch(customer_id):
        raise InvalidContext("context token names an invalid customer")
    return CustomerContext(customer_id, conversation_id, run_id, scopes, token_audience, expires_at)
