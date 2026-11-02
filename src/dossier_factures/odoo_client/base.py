"""Backend interface and read-only safeguard.

The application must never modify Odoo. Every model call goes through
``OdooBackend.call``, which refuses any method outside ``READ_ONLY_METHODS`` before a
backend gets the chance to send anything.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from .errors import ReadOnlyViolation

READ_ONLY_METHODS = frozenset({"context_get", "fields_get", "read", "search_count", "search_read"})

PAGE_SIZE = 200


def ensure_read_only(method: object) -> None:
    if not isinstance(method, str) or method not in READ_ONLY_METHODS:
        raise ReadOnlyViolation(method)


def chunks(items: Sequence[Any], size: int = PAGE_SIZE) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class OdooBackend(ABC):
    """Read-only access to an Odoo database, independent of the wire protocol."""

    def __init__(self) -> None:
        self.default_context: dict[str, Any] = {}

    def call(self, model: str, method: str, **kwargs: Any) -> Any:
        ensure_read_only(method)
        if self.default_context and "context" not in kwargs:
            kwargs["context"] = dict(self.default_context)
        return self._execute(model, method, kwargs)

    @abstractmethod
    def _execute(self, model: str, method: str, kwargs: dict[str, Any]) -> Any:
        """Send one model call. Implementations must call ``ensure_read_only`` again."""

    @abstractmethod
    def server_version(self) -> dict[str, Any]:
        """Return the server version information."""

    def context_get(self) -> dict[str, Any]:
        return self.call("res.users", "context_get")

    def fields_get(self, model: str, attributes: Iterable[str] = ("type",)) -> dict[str, Any]:
        return self.call(model, "fields_get", attributes=list(attributes))

    def search_count(self, model: str, domain: list) -> int:
        return self.call(model, "search_count", domain=domain)

    def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str],
        order: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"domain": domain, "fields": fields, "offset": offset}
        if order:
            kwargs["order"] = order
        if limit is not None:
            kwargs["limit"] = limit
        return self.call(model, "search_read", **kwargs)

    def search_read_all(
        self, model: str, domain: list, fields: list[str], order: str = "id asc"
    ) -> list[dict[str, Any]]:
        """Read every matching record, page by page."""
        records: list[dict[str, Any]] = []
        while True:
            page = self.search_read(
                model, domain, fields, order=order, limit=PAGE_SIZE, offset=len(records)
            )
            records.extend(page)
            if len(page) < PAGE_SIZE:
                return records

    def read(self, model: str, ids: Sequence[int], fields: list[str]) -> list[dict[str, Any]]:
        """Read records by id, splitting long id lists into several requests."""
        records: list[dict[str, Any]] = []
        for batch in chunks(sorted(set(ids))):
            records.extend(self.call(model, "read", ids=list(batch), fields=fields))
        return records
