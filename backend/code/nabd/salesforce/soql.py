"""Safe SOQL building blocks: validated identifiers and escaped literals.

Callers never paste raw input into SOQL. Business identifiers are checked
against strict formats first, and every literal is escaped as well.
"""
from __future__ import annotations

import re
from typing import Iterable

from nabd.salesforce import schema as s

CUSTOMER_ID = re.compile(r"^C\d{3,6}$")
ORDER_NUMBER = re.compile(r"^SE-\d{4,6}$")
KNOWLEDGE_KEY = re.compile(r"^(faq|policy)\.[a-z0-9_]{1,60}$")
EXTERNAL_KEY = re.compile(r"^[A-Za-z0-9_.@:\-]{1,140}$")
SALESFORCE_ID = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
IMPLICIT_FIELDS = {"Id", "Name", "SystemModstamp"}

_ESCAPES = {"\\": "\\\\", "'": "\\'", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t",
            "\b": "\\b", "\f": "\\f"}


class InvalidReference(ValueError):
    """A business identifier did not match its required format."""


def require(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidReference(f"{what} has an invalid format")
    return value


def literal(value: str) -> str:
    return "'" + "".join(_ESCAPES.get(char, char) for char in value) + "'"


def literal_list(values: Iterable[str]) -> str:
    items = [literal(value) for value in values]
    if not items:
        raise ValueError("an IN list needs at least one value")
    return "(" + ", ".join(items) + ")"


def field_path(obj: str, path: str, *, integration: bool) -> str:
    """Check a (possibly relationship) field path against the schema contract.

    With ``integration=True`` every field must be readable by the restricted
    integration user, so the adapter cannot select fields it has no right to.
    """
    current = s.sobject(obj)
    parts = path.split(".")
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if last and part in IMPLICIT_FIELDS:
            break
        if last:
            definition = current.field(part)
            if integration and not (definition.integration_readable or definition.required):
                raise ValueError(f"{current.name}.{part} is not readable by the integration user")
            break
        relationship = next((item for item in current.fields if item.is_relationship
                             and item.relationship == part), None)
        if relationship is None or relationship.reference_to is None:
            raise ValueError(f"{current.name}.{part} is not a relationship in the schema contract")
        current = s.sobject(relationship.reference_to)
    return path


def select(obj: str, *paths: str, integration: bool = True) -> str:
    return ", ".join(field_path(obj, path, integration=integration) for path in paths)
