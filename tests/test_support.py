import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

import dossier_factures
from dossier_factures import odoo_client
from dossier_factures.config import AppConfig
from dossier_factures.credentials import get_api_key
from dossier_factures.formatting import format_amount
from dossier_factures.i18n import t
from dossier_factures.model import NoticeCode, money

PACKAGE = Path(dossier_factures.__file__).parent
CATALOG = json.loads((PACKAGE / "i18n" / "ca.json").read_text(encoding="utf-8"))


def test_every_literal_key_used_in_the_code_exists():
    pattern = re.compile(r"""\bt\(\s*["']([a-z_]+\.[a-z_]+)["']""")
    for path in PACKAGE.rglob("*.py"):
        for key in pattern.findall(path.read_text(encoding="utf-8")):
            assert key in CATALOG, f"{key} in {path.name}"


def test_every_error_has_a_message():
    for name in dir(odoo_client.errors):
        item = getattr(odoo_client.errors, name)
        if isinstance(item, type) and issubclass(item, odoo_client.OdooError):
            assert item.i18n_key in CATALOG, name


def test_every_notice_has_a_message():
    for code in NoticeCode:
        assert f"notice.{code}" in CATALOG


def test_every_odoo_payment_state_has_a_label():
    for state in (
        "not_paid",
        "in_payment",
        "paid",
        "partial",
        "reversed",
        "blocked",
        "invoicing_legacy",
    ):
        assert f"payment_state.{state}" in CATALOG
    assert t("payment_state.in_payment") == "En procés de pagament"


def test_unknown_key_returns_the_key():
    assert t("does.not_exist") == "does.not_exist"


def test_parameters_are_inserted():
    assert "3" in t("notice.other_attachments", count=3)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.005, "1.01"), (0.1 + 0.2, "0.30"), (46542.8, "46542.80"), (None, "0.00"), (-40, "-40.00")],
)
def test_money_rounds_half_up(value, expected):
    assert money(value) == Decimal(expected)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("51130.41", "51.130,41"), ("0.5", "0,50"), ("-40", "-40,00"), ("1234567.8", "1.234.567,80")],
)
def test_format_amount(amount, expected):
    assert format_amount(Decimal(amount)) == expected


def test_config_round_trip_and_no_secret(tmp_path):
    path = tmp_path / "sub" / "config.json"
    AppConfig(url="https://x.example.com", payment_form_overrides={"A": "B"}).save(path)
    loaded = AppConfig.load(path)
    assert loaded.url == "https://x.example.com"
    assert loaded.payment_form_overrides == {"A": "B"}
    assert "key" not in path.read_text(encoding="utf-8").lower()


def test_config_tolerates_missing_or_broken_file(tmp_path):
    assert AppConfig.load(tmp_path / "nope.json") == AppConfig()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert AppConfig.load(broken) == AppConfig()


def test_config_ignores_unknown_keys(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"url": "u", "api_key": "x"}', encoding="utf-8")
    assert AppConfig.load(path).url == "u"
    assert not hasattr(AppConfig.load(path), "api_key")


def test_environment_variable_has_priority(monkeypatch):
    monkeypatch.setenv("ODOO_API_KEY", "from-env")
    assert get_api_key("https://x") == "from-env"
