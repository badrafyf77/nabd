"""Text startup/configuration checks; these never call a model or Salesforce."""
import os
from pathlib import Path
import socket
import subprocess
import sys

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from nabd.app import create_app
from nabd.settings import Settings

BACKEND = Path(__file__).resolve().parents[2]


def test_missing_database_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv("NABD_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


@pytest.mark.parametrize("url", [
    "sqlite:///shop.db", "postgresql://localhost", "postgresql:///nabd",
    "postgresql://localhost:invalid/nabd", "postgresql://localhost/nabd#fragment",
])
def test_invalid_database_urls_are_rejected_without_secret_values(url):
    with pytest.raises(ValidationError) as error:
        Settings(database_url=url, _env_file=None)
    assert url not in str(error.value)


def test_secrets_are_redacted_and_url_escaping_is_preserved():
    settings = Settings(database_url="postgresql://nabd:private%25value@localhost/nabd", _env_file=None)
    assert "private" not in repr(settings)
    assert "private" not in settings.model_dump_json()
    assert settings.sqlalchemy_url.password == "private%value"


def test_invalid_pool_bounds_fail_configuration():
    with pytest.raises(ValidationError, match="must not exceed"):
        Settings(
            database_url="postgresql://localhost/nabd", _env_file=None,
            database_pool_min_size=3, database_pool_max_size=1,
        )


def test_environment_configuration_overrides_file(tmp_path, monkeypatch):
    config = tmp_path / ".env.nabd"
    config.write_text("NABD_DATABASE_URL=postgresql://localhost/from_file\n")
    monkeypatch.setenv("NABD_DATABASE_URL", "postgresql://localhost/from_environment")
    assert Settings(_env_file=config).sqlalchemy_url.database == "from_environment"


def test_health_stays_live_when_database_is_unreachable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(
        database_url=f"postgresql://nabd:never-expose-this@127.0.0.1:{port}/nabd",
        database_timeout_seconds=0.1, _env_file=None,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json() == {"service": "nabd", "status": "ok"}
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"service": "nabd", "status": "not_ready"}
        assert "never-expose-this" not in response.text


def test_text_entrypoint_and_reusable_agent_modules_do_not_import_speech():
    script = """
import importlib.abc
import sys
blocked = ('torch', 'torchaudio', 'transformers', 'RealtimeSTT', 'RealtimeTTS',
           'faster_whisper', 'core.session', 'core.pipeline', 'core.nodes',
           'core.voice_runtime_pool')
class NoSpeech(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + '.') for p in blocked):
            raise AssertionError('Speech import: ' + fullname)
sys.meta_path.insert(0, NoSpeech())
from nabd.app import create_app
from agents.graph import add_tool_agent, TraceHandler
from agents.models import build_chat_model
from core.trace import Tracer
assert {r.path for r in create_app().routes} >= {'/healthz', '/readyz'}
print('text imports are independent of speech')
"""
    env = {**os.environ, "PYTHONPATH": str(BACKEND / "code"), "LANGSMITH_TRACING": "false"}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
