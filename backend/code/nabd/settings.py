"""Validated text-runtime settings, isolated from the legacy voice configuration."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NABD_",
        env_file=BACKEND_ROOT / ".env.nabd",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    database_url: SecretStr
    database_pool_min_size: int = Field(default=1, ge=1)
    database_pool_max_size: int = Field(default=5, ge=1)
    database_timeout_seconds: float = Field(default=3.0, ge=0.1, le=60)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            parsed = urlsplit(value.get_secret_value())
            valid = (
                parsed.scheme == "postgresql"
                and parsed.hostname
                and parsed.path not in ("", "/")
                and not parsed.fragment
            )
            # Reading port also rejects malformed ports without leaking the URL.
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("must be a postgresql:// URL with a host and database")
        return value

    @model_validator(mode="after")
    def validate_pool_size(self) -> Settings:
        if self.database_pool_min_size > self.database_pool_max_size:
            raise ValueError("database_pool_min_size must not exceed database_pool_max_size")
        return self

    @property
    def sqlalchemy_url(self) -> URL:
        return make_url(self.database_url.get_secret_value()).set(drivername="postgresql+psycopg")


class SalesforceSettings(BaseSettings):
    """Server-side credentials of the restricted NABD integration user.

    Loaded separately from ``Settings`` so storage-only commands keep working
    without Salesforce, and Salesforce commands never need the database URL.
    """

    model_config = SettingsConfigDict(
        env_prefix="NABD_SALESFORCE_",
        env_file=BACKEND_ROOT / ".env.nabd",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    domain_url: str
    client_id: SecretStr = Field(min_length=1)
    client_secret: SecretStr = Field(min_length=1)
    api_version: str = Field(default="68.0", pattern=r"^\d{2,3}\.0$")
    timeout_seconds: float = Field(default=15.0, ge=1, le=120)

    @field_validator("domain_url")
    @classmethod
    def validate_domain_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host.endswith(".my.salesforce.com")
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("must be the org's https://<domain>.my.salesforce.com My Domain URL")
        return f"https://{host}"


def _env(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=BACKEND_ROOT / ".env.nabd",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


class BusinessSettings(BaseSettings):
    """ShopEasy's business calendar. Return windows and "today" use this timezone."""

    model_config = _env("NABD_BUSINESS_")

    timezone: str = "Europe/London"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("must be an IANA timezone such as Europe/London") from None
        return value

    def today(self) -> date:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(self.timezone)).date()


class ReadMcpSettings(BaseSettings):
    """The read MCP server and the signed customer context the API sends it."""

    model_config = _env("NABD_READ_MCP_")

    url: str = "http://127.0.0.1:8091/mcp"
    host: str = "127.0.0.1"
    port: int = Field(default=8091, ge=1, le=65535)
    # Host header values the server accepts (DNS-rebinding protection).
    allowed_hosts: str = "127.0.0.1:8091,localhost:8091"
    # Shared by the API (mints) and the MCP server (verifies). At least 32 characters.
    context_secret: SecretStr = Field(min_length=32)
    timeout_seconds: float = Field(default=20.0, ge=1, le=120)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("must be an http(s) URL of the MCP endpoint")
        return value

    @property
    def allowed_host_list(self) -> tuple[str, ...]:
        return _csv(self.allowed_hosts)


class WriteMcpSettings(BaseSettings):
    """The write MCP server (case operations) and its own signed-context secret."""

    model_config = _env("NABD_WRITE_MCP_")

    url: str = "http://127.0.0.1:8092/mcp"
    host: str = "127.0.0.1"
    port: int = Field(default=8092, ge=1, le=65535)
    allowed_hosts: str = "127.0.0.1:8092,localhost:8092"
    # Shared by the API (mints) and the write MCP server (verifies); different from the read secret.
    context_secret: SecretStr = Field(min_length=32)
    timeout_seconds: float = Field(default=30.0, ge=1, le=120)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return ReadMcpSettings.validate_url(value)

    @property
    def allowed_host_list(self) -> tuple[str, ...]:
        return _csv(self.allowed_hosts)


class ModelSettings(BaseSettings):
    """OpenAI models of the NABD agents. The key is passed explicitly, never via os.environ."""

    model_config = _env("NABD_")

    openai_api_key: SecretStr = Field(min_length=1)
    supervisor_model: str = "gpt-5.4-mini"
    supervisor_reasoning_effort: str | None = "low"
    orders_model: str = "gpt-5.4-mini"
    orders_reasoning_effort: str | None = "low"
    complaints_model: str = "gpt-5.4-mini"
    complaints_reasoning_effort: str | None = "medium"
    model_timeout_seconds: float = Field(default=30.0, ge=1, le=300)


class ChatSettings(BaseSettings):
    """Demo sign-in, browser access and turn execution limits."""

    model_config = _env("NABD_CHAT_")

    demo_sign_in: bool = True
    session_hours: float = Field(default=12.0, gt=0, le=24 * 7)
    cookie_secure: bool = False
    # Browser origins allowed to send state-changing requests (the Next.js app).
    allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    turn_deadline_seconds: float = Field(default=120.0, ge=5, le=600)
    agent_tool_calls: int = Field(default=5, ge=1, le=20)
    turn_tool_calls: int = Field(default=20, ge=1, le=100)
    run_lease_seconds: float = Field(default=30.0, ge=5, le=300)
    max_run_attempts: int = Field(default=3, ge=1, le=10)
    worker_concurrency: int = Field(default=4, ge=1, le=32)
    max_message_chars: int = Field(default=4000, ge=1, le=20000)
    # Phase 3: case work.
    history_messages: int = Field(default=12, ge=2, le=50)
    review_confidence: float = Field(default=0.6, ge=0, le=1)  # below: General Support + review flag
    reconcile_after_seconds: float = Field(default=30.0, ge=1, le=3600)
    reconcile_interval_seconds: float = Field(default=10.0, ge=1, le=600)
    max_reconcile_attempts: int = Field(default=5, ge=1, le=50)

    @property
    def allowed_origin_list(self) -> tuple[str, ...]:
        return _csv(self.allowed_origins)


class StaffSyncSettings(BaseSettings):
    """Staff follow-up workers; disabled explicitly only for offline development."""

    model_config = _env("NABD_STAFF_SYNC_")
    enabled: bool = True
    poll_seconds: float = Field(default=0.25, ge=0.05, le=5)
    reconcile_seconds: float = Field(default=60, ge=5, le=3600)
    reconnect_max_seconds: float = Field(default=30, ge=1, le=120)
    translation_timeout_seconds: float = Field(default=25, ge=1, le=45)
