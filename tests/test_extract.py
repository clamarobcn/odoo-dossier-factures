import base64
import hashlib
from datetime import date
from decimal import Decimal

import pytest

from dossier_factures.extract import (
    check_connection,
    check_required_fields,
    detect_content_field,
    extract_dossier,
    fetch_attachment_content,
    list_analytic_accounts,
    list_suppliers,
    list_years,
    sort_key,
)
from dossier_factures.extract.analytics import line_percent
from dossier_factures.model import AttachmentRef, ExtractParams, NoticeCode, PaymentKind
from dossier_factures.odoo_client import AttachmentIntegrityError, MissingFieldsError

YEAR = 2099


@pytest.fixture
def dossier(backend):
    return extract_dossier(backend, ExtractParams(YEAR, include_other_expenses=False))


def by_name(dossier, name):
    return next(i for i in dossier.invoices if i.name == name)


def codes(dossier, name):
    return {n.code for n in dossier.notices if n.invoice_name == name}


def test_only_posted_supplier_invoices_of_the_year_and_company(dossier):
    names = {i.name for i in dossier.invoices}
    assert len(names) == 13
    assert "/" not in names
    assert "FC/2098/12/0001" not in names
    assert "FV/2099/01/0001" not in names
    assert "FC/2099/09/0001" not in names


def test_order_is_natural_and_numbers_are_consecutive(dossier):
    names = [i.name for i in dossier.invoices]
    assert names.index("FC/2099/05/999") < names.index("FC/2099/05/1000")
    assert names[-1] == "RFC/2099/03/0001"
    assert [i.order_number for i in dossier.invoices] == list(range(1, 14))


def test_sort_key_handles_missing_name():
    assert sort_key("") == [""]
    assert sort_key("A/9") < sort_key("A/10")


def test_amounts_are_exact_and_positive_for_invoices(dossier):
    invoice = by_name(dossier, "FC/2099/01/0001")
    assert (invoice.untaxed, invoice.tax, invoice.total) == (
        Decimal("100.00"),
        Decimal("21.00"),
        Decimal("121.00"),
    )


def test_refunds_are_negative_including_their_payments(dossier):
    refund = by_name(dossier, "RFC/2099/03/0001")
    assert refund.is_refund
    assert refund.total == Decimal("-40.00")
    assert [p.amount for p in refund.payments] == [Decimal("-40.00")]
    assert refund.payments[0].kind is PaymentKind.CREDIT_NOTE


def test_totals_add_up(dossier):
    assert dossier.total == sum((i.total for i in dossier.invoices), Decimal(0))
    assert (
        dossier.total
        == Decimal("121") * 2
        + 300
        + 700
        + Decimal("60.5")
        - 40
        + 100
        + 80
        + 55
        + 100
        + 30
        + 20
        + 15
    )
    assert dossier.total_untaxed + dossier.total_tax == dossier.total


def test_plain_payment(dossier):
    payment = by_name(dossier, "FC/2099/01/0001").payments[0]
    assert payment.date == date(2099, 1, 20)
    assert (payment.form, payment.journal_name, payment.method) == (
        "Banc",
        "BANC EXEMPLE",
        "Transferència",
    )
    assert payment.kind is PaymentKind.PAYMENT
    assert payment.reference == "PAG/1"


def test_several_payments_are_all_listed_by_date(dossier):
    payments = by_name(dossier, "FC/2099/01/0002").payments
    assert [(p.date, p.form, p.amount) for p in payments] == [
        (date(2099, 2, 20), "Efectiu", Decimal("71.00")),
        (date(2099, 3, 5), "Banc", Decimal("50.00")),
    ]


def test_batch_payment_shows_only_the_part_applied_to_each_invoice(dossier):
    assert by_name(dossier, "FC/2099/02/0001").payments[0].amount == Decimal("300.00")
    assert by_name(dossier, "FC/2099/02/0002").payments[0].amount == Decimal("700.00")


def test_direct_bank_reconciliation_without_account_payment(dossier):
    payment = by_name(dossier, "FC/2099/03/0001").payments[0]
    assert payment.kind is PaymentKind.BANK_STATEMENT
    assert payment.date == date(2099, 5, 3)
    assert payment.form == "Banc"
    assert payment.method == "Conciliació bancària"
    assert payment.reference == "REBUT 123"


def test_credit_note_compensation_and_card_payment(dossier):
    payments = by_name(dossier, "FC/2099/03/0002").payments
    assert {p.form for p in payments} == {"TPV", "Compensat amb rectificativa"}
    assert sum(p.amount for p in payments) == Decimal("100.00")


def test_miscellaneous_entry_is_kept_as_other(dossier):
    payment = by_name(dossier, "FC/2099/05/999").payments[0]
    assert payment.kind is PaymentKind.OTHER
    assert payment.reference == "OPE/1"


def test_exchange_difference_is_not_a_payment(dossier):
    invoice = by_name(dossier, "FC/2099/05/0001")
    assert [p.amount for p in invoice.payments] == [Decimal("95.00")]
    assert NoticeCode.PAYMENT_MISMATCH not in codes(dossier, invoice.name)


def test_foreign_currency_uses_company_amounts_and_warns(dossier):
    invoice = by_name(dossier, "FC/2099/05/0001")
    assert invoice.total == Decimal("100.00")
    assert NoticeCode.FOREIGN_CURRENCY in codes(dossier, invoice.name)


def test_cancelled_payment_is_ignored_and_unreconciled_active_one_warns(dossier):
    invoice = by_name(dossier, "FC/2099/04/0001")
    assert invoice.payments == []
    found = [
        n
        for n in dossier.notices
        if n.invoice_name == invoice.name and n.code is NoticeCode.UNRECONCILED_PAYMENT
    ]
    assert [n.params["payment"] for n in found] == ["PAG/7"]
    assert NoticeCode.NO_PAYMENT in codes(dossier, invoice.name)


def test_in_payment_state_is_preserved(dossier):
    assert by_name(dossier, "FC/2099/04/0002").payment_state == "in_payment"


def test_disagreement_between_sources_warns(dossier):
    assert NoticeCode.PAYMENT_MISMATCH in codes(dossier, "FC/2099/05/1000")
    assert NoticeCode.PAYMENT_MISMATCH not in codes(dossier, "FC/2099/01/0001")


def test_missing_vat_warns(dossier):
    assert by_name(dossier, "FC/2099/03/0001").partner_vat == ""
    assert NoticeCode.MISSING_VAT in codes(dossier, "FC/2099/03/0001")


def test_quarter_comes_from_the_invoice_date(dossier):
    assert by_name(dossier, "FC/2099/01/0001").quarter == 1
    assert by_name(dossier, "FC/2099/01/0002").quarter == 1
    assert by_name(dossier, "FC/2099/02/0001").quarter == 2


def test_form_overrides_win_over_journal_type(backend):
    params = ExtractParams(YEAR, include_other_expenses=False)
    dossier = extract_dossier(backend, params, {"BANC EXEMPLE": "Transferència"})
    assert by_name(dossier, "FC/2099/01/0001").payments[0].form == "Transferència"


def test_main_attachment_is_preferred(dossier):
    assert by_name(dossier, "FC/2099/01/0001").attachment.id == 1


def test_main_attachment_may_be_an_image(dossier):
    assert by_name(dossier, "FC/2099/01/0002").attachment.kind == "image"


def test_without_main_attachment_the_first_pdf_is_used(dossier):
    invoice = by_name(dossier, "FC/2099/02/0001")
    assert invoice.attachment.name == "primer.pdf"
    notice = next(
        n
        for n in dossier.notices
        if n.invoice_name == invoice.name and n.code is NoticeCode.OTHER_ATTACHMENTS
    )
    assert notice.params["count"] == 1


def test_missing_attachment_warns(dossier):
    assert by_name(dossier, "FC/2099/03/0001").attachment is None
    assert NoticeCode.MISSING_ATTACHMENT in codes(dossier, "FC/2099/03/0001")


def test_main_attachment_of_unusable_type_falls_back(backend, scenario):
    move = next(m for m in scenario["account.move"] if m["id"] == 103)
    move["message_main_attachment_id"] = [3, "fe.xml"]
    dossier = extract_dossier(backend, ExtractParams(YEAR, include_other_expenses=False))
    assert by_name(dossier, "FC/2099/02/0001").attachment.name == "primer.pdf"


def test_supplier_filter(backend):
    dossier = extract_dossier(backend, ExtractParams(YEAR, supplier_id=2))
    assert [i.name for i in dossier.invoices] == ["FC/2099/03/0001"]
    assert dossier.invoices[0].order_number == 1
    assert dossier.supplier_name == "Proveïdor Beta"


def test_analytic_filter_computes_share_with_vat(backend):
    params = ExtractParams(YEAR, analytic_account_id=7, include_other_expenses=False)
    dossier = extract_dossier(backend, params)
    assert [i.name for i in dossier.invoices] == ["FC/2099/02/0001"]
    assert dossier.analytic_account_name == "Projecte Set"
    imputation = dossier.invoices[0].imputation
    assert imputation.percent == Decimal("66.67")
    assert imputation.amount == Decimal("200.00")


def test_analytic_filter_combines_with_supplier(backend):
    dossier = extract_dossier(backend, ExtractParams(YEAR, analytic_account_id=7, supplier_id=2))
    assert dossier.invoices == []


def test_no_imputation_without_analytic_filter(dossier):
    assert all(i.imputation is None for i in dossier.invoices)


@pytest.mark.parametrize(
    ("distribution", "account", "expected"),
    [
        ({"7": 100.0}, 7, "100.0"),
        ({"7": 50.0, "8": 50.0}, 7, "50.0"),
        ({"7,8": 100.0}, 8, "100.0"),
        ({"17": 100.0}, 7, "0"),
        ({"7": 30.0, "7,8": 20.0}, 7, "50.0"),
        (None, 7, "0"),
        ({}, 7, "0"),
    ],
)
def test_line_percent(distribution, account, expected):
    assert line_percent(distribution, account) == Decimal(expected)


def test_company_is_the_users_one_when_there_are_several(dossier):
    assert dossier.company.name == "Entitat d'Exemple"
    assert dossier.company.vat == "G00000000"


def test_explicit_company(backend, scenario):
    params = ExtractParams(YEAR, company_id=2, include_other_expenses=False)
    dossier = extract_dossier(backend, params)
    assert [i.name for i in dossier.invoices] == ["FC/2099/09/0001"]
    assert backend.default_context["allowed_company_ids"] == [2]


def test_only_read_methods_were_called(dossier, backend):
    assert {call[1] for call in backend.calls} <= {"search_read", "read", "context_get"}


def test_attachment_content_never_comes_with_the_invoices(dossier, backend):
    for model, _, kwargs in backend.calls:
        if model == "ir.attachment":
            assert "raw" not in kwargs["fields"]


def test_content_field_prefers_datas_and_falls_back_to_raw(backend, scenario):
    assert detect_content_field(backend) == "raw"
    scenario["ir.attachment"][0]["datas"] = "x"
    assert detect_content_field(backend) == "datas"


def test_content_field_missing(backend, scenario):
    for record in scenario["ir.attachment"]:
        del record["raw"]
    with pytest.raises(MissingFieldsError):
        detect_content_field(backend)


def _ref(record, **overrides):
    values = {
        "id": record["id"],
        "name": record["name"],
        "mimetype": record["mimetype"],
        "size": record["file_size"],
        "checksum": record["checksum"],
    }
    return AttachmentRef(**(values | overrides))


def test_attachment_download_is_decoded_one_by_one(backend, scenario):
    record = scenario["ir.attachment"][0]
    content = fetch_attachment_content(backend, _ref(record), "raw")
    assert content == base64.b64decode(record["raw"])
    assert hashlib.sha1(content).hexdigest() == record["checksum"]
    assert backend.calls[-1][2]["ids"] == [record["id"]]


def test_corrupted_download_is_detected(backend, scenario):
    record = scenario["ir.attachment"][0]
    with pytest.raises(AttachmentIntegrityError):
        fetch_attachment_content(backend, _ref(record, checksum="0" * 40), "raw")
    record["raw"] = ""
    with pytest.raises(AttachmentIntegrityError):
        fetch_attachment_content(backend, _ref(record), "raw")


def test_check_connection_reports_the_essentials(backend):
    info = check_connection(backend)
    assert info.version == "saas~99.1"
    assert info.user_login == "prova@example.invalid"
    assert [c.id for c in info.companies] == [1, 2]
    assert info.content_field == "raw"
    assert info.invoice_count > 0


def test_missing_field_after_an_upgrade_is_reported(backend, scenario):
    for record in scenario["account.partial.reconcile"]:
        del record["exchange_move_id"]
    with pytest.raises(MissingFieldsError) as caught:
        check_required_fields(backend)
    assert caught.value.missing == {"account.partial.reconcile": ["exchange_move_id"]}


def test_list_years_goes_from_today_back_to_the_oldest_invoice(backend, scenario):
    for move in scenario["account.move"]:
        if move["invoice_date"]:
            move["invoice_date"] = (
                move["invoice_date"].replace("2098", "2020").replace("2099", "2021")
            )
    years = list_years(backend)
    assert years[0] == date.today().year
    assert years[-1] == 2020
    assert years == sorted(years, reverse=True)


def test_list_suppliers_only_returns_those_of_the_year_ignoring_accents_in_order(backend):
    assert [s["name"] for s in list_suppliers(backend, YEAR)] == [
        "Proveïdor Àlfa",
        "Proveïdor Beta",
    ]
    assert [s["id"] for s in list_suppliers(backend, 1990)] == []


def test_list_analytic_accounts_shows_code(backend):
    names = [a["name"] for a in list_analytic_accounts(backend)]
    assert names == ["[P7] Projecte Set", "Projecte Vuit"]


def test_pagination_reads_everything(backend, scenario):
    template = scenario["res.partner"][0]
    scenario["res.partner"] = [{**template, "id": n} for n in range(1, 451)]
    assert len(backend.search_read_all("res.partner", [], ["name"])) == 450


def test_receipts_can_be_left_out_with_a_notice(backend):
    dossier = extract_dossier(
        backend, ExtractParams(YEAR, include_receipts=False, include_other_expenses=False)
    )
    assert "FC/2099/06/0001" not in {i.name for i in dossier.invoices}
    notice = next(n for n in dossier.notices if n.code is NoticeCode.RECEIPTS_EXCLUDED)
    assert notice.params == {"count": 1, "total": "15,00"}


def test_receipts_are_included_by_default_even_without_a_supplier(dossier):
    receipt = by_name(dossier, "FC/2099/06/0001")
    assert receipt.is_receipt
    assert receipt.total == Decimal("15.00")
    assert receipt.partner_id is None
    assert receipt.partner_name == ""
    assert [p.form for p in receipt.payments] == ["Efectiu"]
    assert NoticeCode.MISSING_SUPPLIER in codes(dossier, receipt.name)
    assert NoticeCode.RECEIPTS_EXCLUDED not in {n.code for n in dossier.notices}
    assert len(dossier.invoices) == 13


def test_excluded_receipts_follow_the_supplier_filter(backend):
    params = ExtractParams(YEAR, supplier_id=2, include_receipts=False)
    dossier = extract_dossier(backend, params)
    assert NoticeCode.RECEIPTS_EXCLUDED not in {n.code for n in dossier.notices}


@pytest.fixture
def full(backend):
    return extract_dossier(backend, ExtractParams(YEAR))


def test_other_expenses_come_after_the_invoices_with_consecutive_numbers(full):
    names = [i.name for i in full.invoices]
    assert names[13:] == [
        "NOM/2099/01/0001",
        "VAR/2099/01/0001",
        "VAR/2099/02/0001",
        "VAR/2099/02/0002",
    ]
    assert [i.order_number for i in full.invoices] == list(range(1, 18))


def test_other_expenses_use_the_expense_lines_and_the_entry_date(full):
    payroll = by_name(full, "NOM/2099/01/0001")
    assert payroll.is_other_expense
    assert payroll.total == payroll.untaxed == Decimal("650.00")
    assert payroll.tax == Decimal("0.00")
    assert payroll.invoice_date == date(2099, 1, 31)
    assert payroll.payment_state == ""


def test_other_years_stay_out(full):
    assert "VAR/2098/01/0001" not in {i.name for i in full.invoices}


def test_liquidity_line_is_the_payment_and_refunds_are_negative(full):
    fee = by_name(full, "VAR/2099/02/0001").payments
    assert [(p.date, p.form, p.amount) for p in fee] == [
        (date(2099, 2, 2), "Banc Exemple", Decimal("12.00"))
    ]
    refund = by_name(full, "VAR/2099/02/0002")
    assert refund.total == Decimal("-30.00")
    assert refund.payments[0].amount == Decimal("-30.00")


def test_entry_without_money_movement_has_no_payment_and_no_notice(full):
    assert by_name(full, "NOM/2099/01/0001").payments == []
    assert NoticeCode.NO_PAYMENT not in codes(full, "NOM/2099/01/0001")


def test_personnel_concept_hides_names_unless_asked(backend, full):
    assert by_name(full, "NOM/2099/01/0001").partner_name == "Despesa de personal"
    assert by_name(full, "VAR/2099/02/0001").partner_name == "Comissió de manteniment"
    shown = extract_dossier(backend, ExtractParams(YEAR, show_personnel_names=True))
    assert by_name(shown, "NOM/2099/01/0001").partner_name == "NÒMINA GENER PERSONA EXEMPLE"


def test_undocumented_expenses_are_summarized_not_listed(full):
    assert NoticeCode.MISSING_ATTACHMENT not in codes(full, "NOM/2099/01/0001")
    notice = next(n for n in full.notices if n.code is NoticeCode.OTHER_EXPENSES)
    assert notice.params == {"count": 4, "total": "1.282,00"}


def test_possible_duplicates_are_reported(full):
    notice = next(n for n in full.notices if n.code is NoticeCode.POSSIBLE_DUPLICATE)
    assert notice.params["names"] == "NOM/2099/01/0001, VAR/2099/01/0001"
    assert notice.params["month"] == "01/2099"


def test_documented_separates_what_goes_in_the_pdf(full):
    assert {i.name for i in full.undocumented_expenses} == {
        "NOM/2099/01/0001",
        "VAR/2099/01/0001",
        "VAR/2099/02/0001",
        "VAR/2099/02/0002",
    }
    assert len(full.documented) == 13
    assert full.total == sum(i.total for i in full.invoices)


def test_entry_with_a_document_does_go_in_the_pdf(backend, scenario):
    move = next(m for m in scenario["account.move"] if m["id"] == 203)
    move["message_main_attachment_id"] = [1, "factura.pdf"]
    dossier = extract_dossier(backend, ExtractParams(YEAR))
    assert "VAR/2099/02/0001" in {i.name for i in dossier.documented}


def test_other_expenses_can_be_left_out_and_skip_supplier_filter(backend):
    without = extract_dossier(backend, ExtractParams(YEAR, include_other_expenses=False))
    assert not [i for i in without.invoices if i.is_other_expense]
    filtered = extract_dossier(backend, ExtractParams(YEAR, supplier_id=2))
    assert not [i for i in filtered.invoices if i.is_other_expense]


def test_other_expenses_follow_the_project_filter(backend):
    dossier = extract_dossier(backend, ExtractParams(YEAR, analytic_account_id=7))
    fee = by_name(dossier, "VAR/2099/02/0001")
    assert fee.imputation.percent == Decimal("100.00")
    assert fee.imputation.amount == Decimal("12.00")
    assert "NOM/2099/01/0001" not in {i.name for i in dossier.invoices}


def test_list_years_includes_a_year_after_the_current_one(backend):
    years = list_years(backend)
    assert years[0] == 2099
    assert date.today().year in years
