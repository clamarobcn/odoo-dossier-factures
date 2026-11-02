# How the data is read from Odoo

Findings from validating the design against a real Odoo Online database (saas~19.3) with read-only queries. No real data is reproduced here.

## Payments

The application reads `account.partial.reconcile` from the invoice's payable lines.

| Source | Result |
|---|---|
| `account.partial.reconcile` | Exact on every invoice, with the amount applied to that invoice. Also covers bank statement lines and credit notes. |
| `invoice_payments_widget` | Same result, but display-oriented and localized. Used only as an automatic cross-check; a disagreement becomes a notice. |
| `matched_payment_ids` | **Not reliable.** It also returned cancelled payments and active payments never reconciled with the invoice. Never used as a source of amounts. |

Active payments linked through `matched_payment_ids` but not reconciled are reported as a notice, because they may be duplicates that need review in Odoo.

The label of `payment_method_line_id` repeats the journal name, so the plain name is read from `account.payment.method.line`.

## Attachments

- `ir.attachment.datas` no longer exists on saas~19.3: reading it returns an error. The content is in `raw`, as base64. The field is detected with `fields_get`, so older versions keep working.
- `ir.attachment.checksum` is the SHA-1 of the content, so every download can be verified.

## Scanned documents

Scanners may declare pages much larger than A4: 2479×3507 points were found, which is really A4 at 300 ppi with a wrong page size. Consequences for the PDF phases:

- Effective resolution cannot be derived from the declared page size alone. Pages are normalized to A4 first.
- Image data can be stored flipped, with the page transformation putting it right. Images must be replaced in place, leaving that transformation untouched.

## API

- API keys last at most three months, so a yearly user always needs a new one.
- JSON-2 authenticates with the key alone: the user name is not sent.
- `read` and `search_read` accept `context`; `allowed_company_ids` selects the company.
- `/web/version` is public and needs no key.
- Each call is its own transaction, so a dossier is not a strict snapshot if someone edits Odoo during generation.

## Not found in that database

These cases are covered only by the synthetic fixtures in `tests/fixtures`: credit notes, several payments on one invoice, one payment covering several invoices, direct bank reconciliation, card journals in use, foreign currency, image attachments, invoices with no document, and invoices split between analytic accounts.
