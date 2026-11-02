import json
from pathlib import Path
from typing import Any

import pytest

from dossier_factures.odoo_client import OdooBackend, ensure_read_only

FIXTURES = Path(__file__).parent / "fixtures"


def _matches(record: dict[str, Any], condition: list | tuple) -> bool:
    field, operator, expected = condition
    value = record[field]
    if isinstance(value, list) and field != "matched_payment_ids":
        value = value[0] if value else False
    if operator == "=":
        return value == expected
    if operator == "in":
        return value in expected
    if value is False or value is None:
        return False
    if operator == ">=":
        return value >= expected
    if operator == "<=":
        return value <= expected
    raise AssertionError(f"Operator not supported by the fake: {operator}")


class FakeBackend(OdooBackend):
    """Serve fixture records and fail on unknown fields, as the real server does."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__()
        self.tables = tables
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def server_version(self) -> dict[str, Any]:
        return {"version": "saas~99.1"}

    def _execute(self, model: str, method: str, kwargs: dict[str, Any]) -> Any:
        ensure_read_only(method)
        self.calls.append((model, method, kwargs))
        records = self.tables.get(model, [])
        if method == "context_get":
            return {"lang": "ca_ES", "uid": 5}
        if method == "fields_get":
            return {name: {"type": "char"} for record in records for name in record}
        if method == "read":
            return [self._project(r, kwargs["fields"]) for r in records if r["id"] in kwargs["ids"]]
        found = [r for r in records if all(_matches(r, c) for c in kwargs.get("domain", []))]
        if method == "search_count":
            return len(found)
        if method == "search_read":
            field, _, direction = (kwargs.get("order") or "id asc").split(",")[0].partition(" ")
            found.sort(key=lambda r: r[field] or "", reverse=direction.strip() == "desc")
            offset = kwargs.get("offset", 0)
            end = offset + kwargs["limit"] if kwargs.get("limit") else None
            return [self._project(r, kwargs["fields"]) for r in found[offset:end]]
        raise AssertionError(method)

    @staticmethod
    def _project(record: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        return {"id": record["id"], **{f: record[f] for f in fields}}


@pytest.fixture
def scenario() -> dict[str, list[dict[str, Any]]]:
    return json.loads((FIXTURES / "odoo_scenario.json").read_text(encoding="utf-8"))


@pytest.fixture
def backend(scenario) -> FakeBackend:
    return FakeBackend(scenario)
