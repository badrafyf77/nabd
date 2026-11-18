"""Validated text-runtime settings, isolated from the legacy voice configuration."""
from __future__ import annotations

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
