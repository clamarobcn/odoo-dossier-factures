"""Domain model, independent of Odoo and of any output format."""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

CENT = Decimal("0.01")


def money(value: float | int | str | Decimal | None) -> Decimal:
    """Convert an Odoo float to an exact two-decimal amount."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


class PaymentKind(StrEnum):
    PAYMENT = "payment"
    BANK_STATEMENT = "bank_statement"
    CREDIT_NOTE = "credit_note"
    OTHER = "other"


class NoticeCode(StrEnum):
    MISSING_ATTACHMENT = "missing_attachment"
    OTHER_ATTACHMENTS = "other_attachments"
    NO_PAYMENT = "no_payment"
    MISSING_VAT = "missing_vat"
    UNRECONCILED_PAYMENT = "unreconciled_payment"
    PAYMENT_MISMATCH = "payment_mismatch"
    FOREIGN_CURRENCY = "foreign_currency"
    MISSING_SUPPLIER = "missing_supplier"
    RECEIPTS_EXCLUDED = "receipts_excluded"
    DOCUMENT_UNREADABLE = "document_unreadable"
    DOCUMENT_REPAIRED = "document_repaired"
    OTHER_EXPENSES = "other_expenses"
    POSSIBLE_DUPLICATE = "possible_duplicate"


@dataclass(frozen=True)
class Notice:
    """Something the person generating the dossier should review."""

    code: NoticeCode
    invoice_name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Payment:
    """One reconciliation against an invoice; ``amount`` is the part applied to that invoice."""

    date: date
    journal_name: str
    journal_type: str
    form: str
    method: str
    amount: Decimal
    kind: PaymentKind
    reference: str = ""


@dataclass(frozen=True)
class AttachmentRef:
    id: int
    name: str
    mimetype: str
    size: int
    checksum: str = ""

    @property
    def kind(self) -> str | None:
        return attachment_kind(self.mimetype, self.name)


def attachment_kind(mimetype: str | None, name: str | None) -> str | None:
    """Return ``"pdf"``, ``"image"`` or ``None`` when the file cannot go in the dossier."""
    mimetype = (mimetype or "").lower()
    name = (name or "").lower()
    if mimetype == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if mimetype in {"image/jpeg", "image/png"} or name.endswith((".jpg", ".jpeg", ".png")):
        return "image"
    return None


@dataclass(frozen=True)
class ProjectImputation:
    """Share of an invoice charged to the filtered analytic account, VAT included."""

    percent: Decimal
    amount: Decimal


@dataclass
class Invoice:
    id: int
    name: str
    ref: str
    partner_id: int | None
    partner_name: str
    partner_vat: str
    invoice_date: date
    untaxed: Decimal
    tax: Decimal
    total: Decimal
    payment_state: str
    move_type: str
    order_number: int = 0
    payments: list[Payment] = field(default_factory=list)
    attachment: AttachmentRef | None = None
    imputation: ProjectImputation | None = None

    @property
    def quarter(self) -> int:
        return (self.invoice_date.month - 1) // 3 + 1

    @property
    def is_refund(self) -> bool:
        return self.move_type == "in_refund"

    @property
    def is_receipt(self) -> bool:
        return self.move_type == "in_receipt"

    @property
    def is_other_expense(self) -> bool:
        return self.move_type == "entry"

    @property
    def amount_paid(self) -> Decimal:
        return sum((p.amount for p in self.payments), Decimal("0.00"))


@dataclass(frozen=True)
class Company:
    id: int
    name: str
    vat: str
    currency: str


@dataclass(frozen=True)
class ExtractParams:
    year: int
    company_id: int | None = None
    analytic_account_id: int | None = None
    supplier_id: int | None = None
    include_receipts: bool = True
    include_other_expenses: bool = True
    show_personnel_names: bool = False


@dataclass
class Dossier:
    company: Company
    params: ExtractParams
    invoices: list[Invoice]
    notices: list[Notice]
    analytic_account_name: str = ""
    supplier_name: str = ""

    @property
    def documented(self) -> list[Invoice]:
        """What goes in the PDF: everything except other expenses that have no document."""
        return [i for i in self.invoices if not i.is_other_expense or i.attachment is not None]

    @property
    def undocumented_expenses(self) -> list[Invoice]:
        return [i for i in self.invoices if i.is_other_expense and i.attachment is None]

    @property
    def total_untaxed(self) -> Decimal:
        return sum((i.untaxed for i in self.invoices), Decimal("0.00"))

    @property
    def total_tax(self) -> Decimal:
        return sum((i.tax for i in self.invoices), Decimal("0.00"))

    @property
    def total(self) -> Decimal:
        return sum((i.total for i in self.invoices), Decimal("0.00"))

    @property
    def total_paid(self) -> Decimal:
        return sum((i.amount_paid for i in self.invoices), Decimal("0.00"))
