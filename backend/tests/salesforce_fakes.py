"""An in-memory stand-in for the parts of Salesforce the seed uses. No network.

It stores records the way Salesforce returns them (numbers as floats, datetimes
as ``2026-11-18T09:00:00.000+0000``) so the seed's value comparison is tested
against realistic round trips.
"""
from __future__ import annotations

import itertools
import re
from datetime import datetime
from typing import Any

from nabd.salesforce import schema as s

_IN_QUERY = re.compile(r"SELECT (?P<fields>.+?) FROM (?P<object>\w+) WHERE (?P<key>\w+) IN \((?P<values>.*)\)$")


def _salesforce_form(sobject: str, name: str, value: Any) -> Any:
    if value is None or name in ("Id", "Name", s.SEED_FINGERPRINT):
        return value
    try:
        kind = s.sobject(sobject).field(name).type
    except KeyError:
        return value
    if kind == "Number":
        return float(value)
    if kind == "DateTime":
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    return value


class FakeSalesforce:
    def __init__(self):
        self.records: dict[str, dict[str, dict[str, Any]]] = {}
        self.writes: list[tuple[str, str, list[dict[str, Any]]]] = []
        self._ids = itertools.count(1)
        self.queues = {queue.developer_name: f"00G{index:015d}" for index, queue in enumerate(s.QUEUES)}

    def _new_id(self, sobject: str) -> str:
        return f"{sobject[:3].upper()}{next(self._ids):015d}"

    def _store(self, sobject: str, record: dict[str, Any], record_id: str | None = None) -> dict[str, Any]:
        table = self.records.setdefault(sobject, {})
        record_id = record_id or self._new_id(sobject)
        row = table.setdefault(record_id, {"Id": record_id})
        for name, value in record.items():
            if name != "attributes":
                row[name] = _salesforce_form(sobject, name, value)
        return row

    def find(self, sobject: str, key: str) -> dict[str, Any]:
        key_field = s.sobject(sobject).seed_key
        return next(row for row in self.records[sobject].values() if row.get(key_field) == key)

    async def query(self, soql: str) -> list[dict[str, Any]]:
        if soql.startswith("SELECT Id, DeveloperName FROM Group"):
            return [{"Id": queue_id, "DeveloperName": name} for name, queue_id in self.queues.items()]
        match = _IN_QUERY.match(soql)
        assert match, soql
        fields = [name.strip() for name in match["fields"].split(",")]
        keys = set(re.findall(r"'((?:[^'\\]|\\.)*)'", match["values"]))
        rows = self.records.get(match["object"], {}).values()
        return [{name: row.get(name) for name in fields} for row in rows if row.get(match["key"]) in keys]

    async def upsert(self, sobject: str, external_field: str, records: list[dict[str, Any]]):
        self.writes.append(("upsert", sobject, records))
        results = []
        for record in records:
            existing = next((row for row in self.records.get(sobject, {}).values()
                             if row.get(external_field) == record[external_field]), None)
            if existing is not None:
                for name in record:
                    field = s.sobject(sobject).field(name) if name not in ("attributes", "Name") else None
                    if field is not None and field.type == "MasterDetail":
                        raise AssertionError(f"master-detail {name} sent on update")
            row = self._store(sobject, record, existing["Id"] if existing else None)
            results.append({"id": row["Id"], "success": True, "created": existing is None})
        return results

    async def create(self, sobject: str, records: list[dict[str, Any]]):
        self.writes.append(("create", sobject, records))
        return [{"id": self._store(sobject, record)["Id"], "success": True} for record in records]

    async def update(self, sobject: str, records: list[dict[str, Any]]):
        self.writes.append(("update", sobject, records))
        for record in records:
            self._store(sobject, {k: v for k, v in record.items() if k != "Id"}, record["Id"])
        return [{"id": record["Id"], "success": True} for record in records]
