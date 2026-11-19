"""Bounded, operator-only tool results and model usage for one turn attempt."""
from __future__ import annotations

import json
import re
from typing import Any

MAX_ITEMS = 180
MAX_VALUE_BYTES = 12_000
_SECRET_KEY = re.compile(r"authorization|cookie|credential|password|secret|(?:^|_)(?:api_key|token)$", re.I)
_SECRET_TEXT = re.compile(r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]+|\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
                          r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
                          r"\bv1\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}\b")


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[redacted]" if _SECRET_KEY.search(key) else _redacted(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_redacted(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[redacted]", value).replace("\x00", "\ufffd")
    return value


def bounded(value: Any) -> Any:
    encoded = json.dumps(_redacted(value), ensure_ascii=False, default=str).encode("utf-8", errors="replace")
    if len(encoded) > MAX_VALUE_BYTES:
        return {"truncated": True, "preview": encoded[:MAX_VALUE_BYTES].decode("utf-8", errors="ignore")}
    return json.loads(encoded)


def token_usage(usage: Any) -> dict[str, int]:
    usage = usage or {}
    return {
        "input": usage.get("input_tokens", 0),
        "cached_input": (usage.get("input_token_details") or {}).get("cache_read", 0),
        "output": usage.get("output_tokens", 0),
        "reasoning": (usage.get("output_token_details") or {}).get("reasoning", 0),
    }
