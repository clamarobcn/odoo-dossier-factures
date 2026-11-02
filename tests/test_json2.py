import logging

import pytest
import requests

from dossier_factures.odoo_client import (
    AccessOrPlanError,
    ApiNotFoundError,
    InsecureUrlError,
    InvalidApiKeyError,
    InvalidUrlError,
    Json2Backend,
    NetworkError,
    OdooApiError,
    ServerBusyError,
    UnexpectedRedirectError,
    guess_database,
    normalize_base_url,
)

SECRET = "s3cr3t-api-key"


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text_only=False):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._text_only = text_only

    def json(self):
        if self._text_only:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    get = post


def make(*responses, **kwargs):
    session = FakeSession(*responses)
    sleeps = []
    backend = Json2Backend(
        "https://demo.example.com", SECRET, "demo", session=session, sleep=sleeps.append, **kwargs
    )
    return backend, session, sleeps


def test_request_follows_the_documented_format():
    backend, session, _ = make(FakeResponse(body=[{"id": 1}]))
    assert backend.search_read("res.partner", [("id", "=", 1)], ["name"], limit=5) == [{"id": 1}]
    url, kwargs = session.requests[0]
    assert url == "https://demo.example.com/json/2/res.partner/search_read"
    assert kwargs["headers"]["Authorization"] == f"bearer {SECRET}"
    assert kwargs["headers"]["X-Odoo-Database"] == "demo"
    assert kwargs["headers"]["Content-Type"].startswith("application/json")
    assert kwargs["json"] == {
        "domain": [("id", "=", 1)],
        "fields": ["name"],
        "offset": 0,
        "limit": 5,
    }
    assert kwargs["allow_redirects"] is False


def test_database_header_is_omitted_when_empty():
    session = FakeSession(FakeResponse(body={}))
    Json2Backend("https://demo.example.com", SECRET, session=session).context_get()
    assert "X-Odoo-Database" not in session.requests[0][1]["headers"]


def test_default_context_is_sent():
    backend, session, _ = make(FakeResponse(body=[]))
    backend.default_context["allowed_company_ids"] = [3]
    backend.read("res.partner", [1], ["name"])
    assert session.requests[0][1]["json"]["context"] == {"allowed_company_ids": [3]}


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (
            401,
            {"name": "werkzeug.exceptions.Unauthorized", "message": "Invalid apikey"},
            InvalidApiKeyError,
        ),
        (403, {"name": "x", "message": "plan"}, AccessOrPlanError),
        (500, {"name": "odoo.exceptions.AccessError", "message": "no"}, AccessOrPlanError),
        (404, {}, ApiNotFoundError),
        (500, {"name": "builtins.ValueError", "message": "Invalid field"}, OdooApiError),
        (302, {}, UnexpectedRedirectError),
    ],
)
def test_errors_are_mapped(status, body, error):
    backend, _, sleeps = make(FakeResponse(status, body))
    with pytest.raises(error):
        backend.context_get()
    assert sleeps == []


def test_html_error_page_is_handled():
    backend, _, _ = make(FakeResponse(500, text_only=True))
    with pytest.raises(OdooApiError):
        backend.context_get()


def test_server_traceback_is_not_kept():
    body = {"name": "builtins.ValueError", "message": "boom", "debug": "Traceback secret path"}
    backend, _, _ = make(FakeResponse(500, body))
    with pytest.raises(OdooApiError) as caught:
        backend.context_get()
    assert "Traceback" not in str(caught.value)
    assert "Traceback" not in caught.value.detail


def test_retries_busy_server_then_succeeds():
    backend, session, sleeps = make(
        FakeResponse(503), FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(body={})
    )
    assert backend.context_get() == {}
    assert len(session.requests) == 3
    assert sleeps[1] == 7


def test_retry_after_is_capped():
    backend, _, sleeps = make(
        FakeResponse(429, headers={"Retry-After": "9999"}), FakeResponse(body={})
    )
    backend.context_get()
    assert sleeps == [60]


def test_retries_network_errors_then_succeeds():
    backend, session, _ = make(
        requests.ConnectionError(), requests.Timeout(), FakeResponse(body={})
    )
    assert backend.context_get() == {}
    assert len(session.requests) == 3


def test_gives_up_after_max_attempts():
    backend, session, sleeps = make(*[requests.Timeout() for _ in range(3)], max_attempts=3)
    with pytest.raises(NetworkError):
        backend.context_get()
    assert len(session.requests) == 3
    assert len(sleeps) == 2

    backend, session, _ = make(*[FakeResponse(503) for _ in range(3)], max_attempts=3)
    with pytest.raises(ServerBusyError):
        backend.context_get()


def test_api_key_is_never_exposed(caplog):
    backend, _, _ = make(FakeResponse(401, {"name": "x", "message": "bad"}))
    assert SECRET not in repr(backend)
    with caplog.at_level(logging.DEBUG), pytest.raises(InvalidApiKeyError) as caught:
        backend.context_get()
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.detail
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("demo.odoo.com", "https://demo.odoo.com"),
        ("https://demo.odoo.com/", "https://demo.odoo.com"),
        ("  https://demo.odoo.com/odoo/action-1?x=1 ", "https://demo.odoo.com"),
        ("http://localhost:8069/web", "http://localhost:8069"),
    ],
)
def test_url_normalization(raw, expected):
    assert normalize_base_url(raw) == expected


def test_plain_http_is_refused_outside_localhost():
    with pytest.raises(InsecureUrlError):
        normalize_base_url("http://demo.odoo.com")


@pytest.mark.parametrize("raw", ["", "   ", "ftp://demo", "https://"])
def test_invalid_urls(raw):
    with pytest.raises(InvalidUrlError):
        normalize_base_url(raw)


def test_guess_database():
    assert guess_database("https://demo.odoo.com/odoo") == "demo"
    assert guess_database("https://erp.example.com") == ""
