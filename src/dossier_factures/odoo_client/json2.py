"""Backend for the JSON-2 external API (Odoo 19 and later)."""

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from .. import __version__
from .base import OdooBackend, ensure_read_only
from .errors import (
    AccessOrPlanError,
    ApiNotFoundError,
    InsecureUrlError,
    InvalidApiKeyError,
    InvalidUrlError,
    NetworkError,
    OdooApiError,
    ServerBusyError,
    UnexpectedRedirectError,
)

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 180
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.5
MAX_RETRY_AFTER = 60
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def normalize_base_url(url: str) -> str:
    """Reduce whatever the user pasted to ``scheme://host[:port]``."""
    url = (url or "").strip()
    if not url:
        raise InvalidUrlError(url)
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or " " in parts.netloc:
        raise InvalidUrlError(url)
    if parts.scheme == "http" and parts.netloc.split(":")[0] not in LOCAL_HOSTS:
        # The API key would travel in clear text.
        raise InsecureUrlError(url)
    return f"{parts.scheme}://{parts.netloc}"


def database_from_url(url: str) -> str:
    """The server finds the database from its address; only a shared server needs ``?db=``."""
    url = (url or "").strip()
    query = urlsplit(url if "://" in url else f"https://{url}").query
    return parse_qs(query).get("db", [""])[0]


class Json2Backend(OdooBackend):
    def __init__(
        self,
        url: str,
        api_key: str,
        database: str = "",
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        super().__init__()
        self.base_url = normalize_base_url(url)
        self.database = database.strip()
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._max_attempts = max_attempts

    def __repr__(self) -> str:
        return f"Json2Backend(base_url={self.base_url!r}, database={self.database!r})"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"bearer {self._api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": f"DossierFactures/{__version__}",
        }
        if self.database:
            headers["X-Odoo-Database"] = self.database
        return headers

    def _execute(self, model: str, method: str, kwargs: dict[str, Any]) -> Any:
        ensure_read_only(method)
        url = f"{self.base_url}/json/2/{model}/{method}"
        logger.debug("POST /json/2/%s/%s", model, method)
        response = self._with_retries(
            lambda: self._session.post(
                url,
                headers=self._headers(),
                json=kwargs,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=False,
            )
        )
        return self._parse(response)

    def server_version(self) -> dict[str, Any]:
        response = self._with_retries(
            lambda: self._session.get(
                f"{self.base_url}/web/version",
                headers={"User-Agent": f"DossierFactures/{__version__}"},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=False,
            )
        )
        return self._parse(response)

    def _with_retries(self, send: Callable[[], Any]) -> Any:
        for attempt in range(1, self._max_attempts + 1):
            last = attempt == self._max_attempts
            try:
                response = send()
            except (requests.ConnectionError, requests.Timeout) as exc:
                logger.info("Network problem on attempt %d: %s", attempt, type(exc).__name__)
                if last:
                    raise NetworkError(type(exc).__name__) from exc
                self._sleep(BACKOFF_BASE**attempt)
                continue
            if response.status_code not in RETRYABLE_STATUS:
                return response
            logger.info("Status %d on attempt %d", response.status_code, attempt)
            if last:
                raise ServerBusyError(str(response.status_code))
            self._sleep(self._retry_delay(response, attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(response: Any, attempt: int) -> float:
        try:
            return min(float(response.headers.get("Retry-After", "")), MAX_RETRY_AFTER)
        except ValueError:
            return BACKOFF_BASE**attempt

    @staticmethod
    def _parse(response: Any) -> Any:
        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise OdooApiError("non-JSON response") from exc
        if 300 <= status < 400:
            raise UnexpectedRedirectError(str(status))
        try:
            body = response.json()
        except ValueError:
            body = {}
        # "debug" holds a server traceback and is deliberately ignored.
        name = body.get("name", "") if isinstance(body, dict) else ""
        message = body.get("message", "") if isinstance(body, dict) else ""
        detail = f"{status} {name}: {message}".strip()
        if status == 401:
            raise InvalidApiKeyError(detail)
        if status == 403 or name.endswith("AccessError") or name.endswith("AccessDenied"):
            raise AccessOrPlanError(detail)
        if status == 404:
            raise ApiNotFoundError(detail)
        raise OdooApiError(detail)
