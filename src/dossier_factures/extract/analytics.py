"""Filter invoices by analytic account and compute the share charged to it."""

from collections import defaultdict
from decimal import Decimal
from typing import Any

from ..model import ProjectImputation, money
from ..odoo_client import OdooBackend, chunks

LINE_FIELDS = ["move_id", "analytic_distribution", "price_total"]


def line_percent(distribution: dict[str, float] | None, account_id: int) -> Decimal:
    """Percentage of a line charged to an account.

    Distribution keys are one account id or several joined by commas ("2,4"), when one
    share is charged to accounts of different analytic plans at the same time.
    """
    wanted = str(account_id)
    return sum(
        (
            Decimal(str(percent))
            for key, percent in (distribution or {}).items()
            if wanted in key.split(",")
        ),
        Decimal(0),
    )


def compute_imputations(
    backend: OdooBackend, moves: list[dict[str, Any]], account_id: int
) -> dict[int, ProjectImputation]:
    """Return the imputation of each move charged to the account; others are left out.

    The amount includes VAT: the association cannot deduct it, so its cost is what it paid.
    """
    charged: dict[int, Decimal] = defaultdict(Decimal)
    for batch in chunks([m["id"] for m in moves]):
        for line in backend.search_read_all(
            "account.move.line",
            [("move_id", "in", list(batch)), ("display_type", "=", "product")],
            LINE_FIELDS,
        ):
            percent = line_percent(line["analytic_distribution"], account_id)
            if percent:
                charged[line["move_id"][0]] += Decimal(str(line["price_total"])) * percent / 100

    result: dict[int, ProjectImputation] = {}
    for move in moves:
        if move["id"] not in charged:
            continue
        document_total = Decimal(str(move["amount_total"]))
        share = charged[move["id"]] / document_total if document_total else Decimal(0)
        company_total = money(abs(move["amount_total_signed"]))
        sign = -1 if move["move_type"] == "in_refund" else 1
        result[move["id"]] = ProjectImputation(
            percent=(share * 100).quantize(Decimal("0.01")),
            amount=money(company_total * share) * sign,
        )
    return result
