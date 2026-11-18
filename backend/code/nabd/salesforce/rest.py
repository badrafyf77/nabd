"""Minimal Salesforce REST client with server-side authentication.

Two token sources exist:

* ``ClientCredentialsAuth`` - the backend's own restricted integration user
  (External Client App, OAuth 2.0 client credentials flow);
* ``CliOrgAuth`` - a developer's ``sf`` CLI session, used only by deployment
  and seed commands run on a workstation.

Tokens never appear in exceptions, ``repr`` output or logs. Failures raise
typed errors; nothing here falls back to local or fabricated business data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

logger = logging.getLogger(__name__)

COLLECTION_LIMIT = 200  # Salesforce sObject Collections accept up to 200 records.


class SalesforceError(Exception):
    """Base class. Messages contain error codes, never credentials or tokens."""

    def __init__(self, message: str, *, status: int | None = None, codes: tuple[str, ...] = ()):
        super().__init__(message)
        self.status = status
        self.codes = codes


class SalesforceAuthError(SalesforceError):
    """Credentials were rejected or a session could not be established."""


class SalesforceUnavailable(SalesforceError):
    """Network failure, timeout or a Salesforce server error. Retry later."""


class SalesforceRequestError(SalesforceError):
    """Salesforce refused the request (permissions, validation, malformed query)."""


@dataclass(frozen=True)
class AccessToken:
    value: SecretStr
    instance_url: str

    def __repr__(self) -> str:
        return f"AccessToken(instance_url={self.instance_url!r}, value='**********')"


class TokenSource(Protocol):
    async def token(self, *, refresh: bool = False) -> AccessToken: ...


def _validated_instance(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/"):
        raise SalesforceAuthError("Salesforce returned an unusable instance URL.")
    return f"https://{parts.netloc}"


@dataclass
class ClientCredentialsAuth:
    """OAuth 2.0 client credentials flow against the org's My Domain."""

    domain_url: str
    client_id: SecretStr
    client_secret: SecretStr
    http: httpx.AsyncClient
    _cached: AccessToken | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def token(self, *, refresh: bool = False) -> AccessToken:
        async with self._lock:
            if self._cached is not None and not refresh:
                return self._cached
            self._cached = None
            try:
                response = await self.http.post(
                    f"{self.domain_url.rstrip('/')}/services/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id.get_secret_value(),
                        "client_secret": self.client_secret.get_secret_value(),
                    },
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as error:
                raise SalesforceUnavailable(
                    f"Salesforce token endpoint unreachable ({type(error).__name__})."
                ) from None
            if response.status_code >= 500:
                raise SalesforceUnavailable("Salesforce token endpoint failed.", status=response.status_code)
            payload = _json(response)
            if response.status_code != 200 or "access_token" not in payload:
                # The OAuth error code (for example invalid_client) is safe to report.
                code = str(payload.get("error") or "authentication_failed")[:60]
                raise SalesforceAuthError(
                    f"Salesforce rejected the integration credentials ({code}).",
                    status=response.status_code, codes=(code,),
                )
            self._cached = AccessToken(
                SecretStr(str(payload["access_token"])), _validated_instance(str(payload["instance_url"]))
            )
            return self._cached


@dataclass
class CliOrgAuth:
    """Reuse an authenticated ``sf`` CLI org for developer-only commands."""

    target_org: str
    _cached: AccessToken | None = field(default=None, init=False, repr=False)
    org_id: str | None = field(default=None, init=False)
    username: str | None = field(default=None, init=False)

    async def token(self, *, refresh: bool = False) -> AccessToken:
        if self._cached is None or refresh:
            self._cached = await asyncio.to_thread(self._display)
        return self._cached

    def _display(self) -> AccessToken:
        executable = shutil.which("sf")
        if executable is None:
            raise SalesforceAuthError("The Salesforce CLI (sf) is not installed.")
        def run(*arguments: str) -> dict[str, Any]:
            completed = subprocess.run(
                [executable, *arguments, "--target-org", self.target_org, "--json"],
                capture_output=True, text=True, timeout=120, check=False,
            )
            return json.loads(completed.stdout)["result"]

        try:
            # `org display` refreshes an expired session and names the org; current CLI
            # versions only reveal the token itself through `org auth show-access-token`.
            details = run("org", "display")
            token = run("org", "auth", "show-access-token")["accessToken"]
            instance = details["instanceUrl"]
        except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            raise SalesforceAuthError(
                f"The sf CLI has no usable session for '{self.target_org}'. "
                f"Run: sf org login web --alias {self.target_org}"
            ) from None
        self.org_id = details.get("id")
        self.username = details.get("username")
        return AccessToken(SecretStr(token), _validated_instance(instance))


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def _error_details(payload: Any) -> tuple[tuple[str, ...], str]:
    """Extract Salesforce error codes and the first message from any error shape."""
    items = payload if isinstance(payload, list) else [payload]
    codes, messages = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        for error in item.get("errors") or [item]:
            if not isinstance(error, dict):
                continue
            code = error.get("errorCode") or error.get("statusCode")
            if code:
                codes.append(str(code))
            if error.get("message"):
                messages.append(str(error["message"]))
    return tuple(codes), (messages[0][:300] if messages else "")


@dataclass
class SalesforceClient:
    auth: TokenSource
    http: httpx.AsyncClient
    api_version: str

    @property
    def base_path(self) -> str:
        return f"/services/data/v{self.api_version}"

    async def request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                      json_body: Any = None, raw_json: bytes | None = None) -> Any:
        """Send one request, refreshing the session once if Salesforce expired it.

        ``raw_json`` sends exact bytes, for callers whose request body must be
        byte-identical on every retry (the case gateway hashes it).
        """
        headers = {"Accept": "application/json"}
        if raw_json is not None:
            headers["Content-Type"] = "application/json"
        for attempt in (1, 2):
            token = await self.auth.token(refresh=attempt == 2)
            try:
                response = await self.http.request(
                    method, f"{token.instance_url}{path}", params=params, json=json_body, content=raw_json,
                    headers={**headers, "Authorization": f"Bearer {token.value.get_secret_value()}"},
                )
            except httpx.HTTPError as error:
                raise SalesforceUnavailable(f"Salesforce request failed ({type(error).__name__}).") from None
            if response.status_code == 401 and attempt == 1:
                logger.info("Salesforce session expired; requesting a new token")
                continue
            if response.status_code == 401:
                raise SalesforceAuthError("Salesforce rejected a freshly issued session.", status=401)
            if response.status_code >= 500:
                raise SalesforceUnavailable("Salesforce returned a server error.", status=response.status_code)
            if response.status_code == 204:
                return None
            payload = _json(response)
            if response.status_code >= 400:
                codes, message = _error_details(payload)
                raise SalesforceRequestError(
                    f"Salesforce refused {method} {path.split('?')[0]}: {', '.join(codes) or 'error'}"
                    + (f" - {message}" if message else ""),
                    status=response.status_code, codes=codes,
                )
            return payload
        raise AssertionError("unreachable")

    async def query(self, soql: str) -> list[dict[str, Any]]:
        """Run a SOQL query and follow every result page."""
        payload = await self.request("GET", f"{self.base_path}/query", params={"q": soql})
        records = list(payload.get("records", []))
        while not payload.get("done", True) and payload.get("nextRecordsUrl"):
            payload = await self.request("GET", payload["nextRecordsUrl"])
            records.extend(payload.get("records", []))
        return [_strip_attributes(record) for record in records]

    async def _collection(self, method: str, path: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for start in range(0, len(records), COLLECTION_LIMIT):
            chunk = records[start:start + COLLECTION_LIMIT]
            response = await self.request(method, path, json_body={"allOrNone": True, "records": chunk})
            failures = [item for item in response if not item.get("success")]
            if failures:
                codes, message = _error_details(failures)
                raise SalesforceRequestError(
                    f"Salesforce rejected {len(failures)} record(s): {', '.join(sorted(set(codes)))}"
                    + (f" - {message}" if message else ""),
                    codes=codes,
                )
            results.extend(response)
        return results

    async def upsert(self, sobject: str, external_field: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        typed = [{"attributes": {"type": sobject}, **record} for record in records]
        return await self._collection(
            "PATCH", f"{self.base_path}/composite/sobjects/{sobject}/{external_field}", typed)

    async def create(self, sobject: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        typed = [{"attributes": {"type": sobject}, **record} for record in records]
        return await self._collection("POST", f"{self.base_path}/composite/sobjects", typed)

    async def update(self, sobject: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        typed = [{"attributes": {"type": sobject}, **record} for record in records]
        return await self._collection("PATCH", f"{self.base_path}/composite/sobjects", typed)


def _strip_attributes(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {key: _strip_attributes(item) for key, item in value.items() if key != "attributes"}
        if set(cleaned) == {"totalSize", "done", "records"}:  # child relationship subquery
            return [_strip_attributes(item) for item in cleaned["records"]]
        return cleaned
    if isinstance(value, list):
        return [_strip_attributes(item) for item in value]
    return value
