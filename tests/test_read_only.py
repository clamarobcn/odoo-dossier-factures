"""The application must never be able to modify Odoo."""

import ast
from pathlib import Path

import pytest

import dossier_factures
from dossier_factures.odoo_client import (
    READ_ONLY_METHODS,
    Json2Backend,
    ReadOnlyViolation,
    ensure_read_only,
)

PACKAGE = Path(dossier_factures.__file__).parent
FORBIDDEN = [
    "create",
    "write",
    "unlink",
    "copy",
    "action_post",
    "button_draft",
    "button_cancel",
    "action_register_payment",
    "message_post",
    "generate",
    "name_create",
    "web_save",
    "toggle_active",
    "execute_kw",
    "READ",
    "read ",
    "",
    None,
    ["read"],
]


class ExplodingSession:
    def post(self, *args, **kwargs):
        raise AssertionError("A forbidden method reached the network")

    def get(self, *args, **kwargs):
        raise AssertionError("A forbidden method reached the network")


def test_whitelist_is_exactly_the_agreed_read_methods():
    assert (
        frozenset({"context_get", "fields_get", "read", "search_count", "search_read"})
        == READ_ONLY_METHODS
    )


@pytest.mark.parametrize("method", FORBIDDEN)
def test_forbidden_method_is_rejected_by_the_guard(method):
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(method)


@pytest.mark.parametrize("method", FORBIDDEN)
def test_forbidden_method_never_reaches_the_network(method):
    backend = Json2Backend("https://x.example.com", "key", session=ExplodingSession())
    with pytest.raises(ReadOnlyViolation):
        backend.call("account.move", method, ids=[1])


@pytest.mark.parametrize("method", FORBIDDEN)
def test_guard_also_protects_the_transport_layer(method):
    backend = Json2Backend("https://x.example.com", "key", session=ExplodingSession())
    with pytest.raises(ReadOnlyViolation):
        backend._execute("account.move", method, {"ids": [1]})


def _python_files():
    return sorted(PACKAGE.rglob("*.py"))


def test_only_the_json2_backend_uses_http_libraries():
    http_libraries = {"requests", "httpx", "urllib", "urllib3", "http", "xmlrpc", "socket"}
    offenders = []
    for path in _python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            if any(name.split(".")[0] in http_libraries for name in names):
                offenders.append(path.name)
    assert set(offenders) <= {"json2.py"}, offenders


def test_all_model_calls_in_the_source_use_whitelisted_methods():
    """Every literal method passed to ``.call(model, method)`` must be whitelisted."""
    for path in _python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
            ):
                assert node.args[1].value in READ_ONLY_METHODS, f"{path.name}:{node.lineno}"
