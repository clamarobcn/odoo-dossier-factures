"""Command line interface. Takes the API key from ODOO_API_KEY or the credential vault."""

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .config import AppConfig
from .credentials import get_api_key
from .excel_builder import build_workbook, excel_filename
from .extract import (
    check_connection,
    extract_dossier,
    list_analytic_accounts,
    list_suppliers,
    list_years,
)
from .formatting import format_amount
from .i18n import t
from .model import ExtractParams
from .odoo_client import Json2Backend, OdooError, guess_database, normalize_base_url
from .paths import new_output_folder


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dossier-factures", description=t("cli.description"))
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--url", help="Adreça d'Odoo, p. ex. https://entitat.odoo.com")
    parser.add_argument("--db", help="Base de dades (per defecte, la de l'adreça)")
    parser.add_argument("--user", help="Usuari o correu (per trobar la clau desada)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Mostra el detall tècnic")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("test", help="Prova la connexió")
    commands.add_parser("years", help="Llista els anys disponibles")
    commands.add_parser("projects", help="Llista els projectes (comptes analítics)")
    suppliers = commands.add_parser("suppliers", help="Llista els proveïdors d'un any")
    suppliers.add_argument("--year", type=int, required=True)

    extract = commands.add_parser("extract", help="Llegeix les dades sense generar documents")
    extract.add_argument("--year", type=int, required=True)
    extract.add_argument("--project", type=int, help="Id del compte analític")
    extract.add_argument("--supplier", type=int, help="Id del proveïdor")
    extract.add_argument("--json", type=Path, help="Desa les dades en un fitxer JSON")
    extract.add_argument("--include-receipts", action="store_true", help=t("cli.help.receipts"))

    excel = commands.add_parser("excel", help=t("cli.help.excel"))
    excel.add_argument("--year", type=int, required=True)
    excel.add_argument("--project", type=int, help="Id del compte analític")
    excel.add_argument("--supplier", type=int, help="Id del proveïdor")
    excel.add_argument("--out", type=Path, required=True, help=t("cli.help.out"))
    excel.add_argument("--include-receipts", action="store_true", help=t("cli.help.receipts"))
    return parser


def _backend(args: argparse.Namespace, config: AppConfig) -> Json2Backend:
    url = normalize_base_url(args.url or config.url)
    user = args.user or config.user
    api_key = get_api_key(url, user)
    if not api_key:
        raise SystemExit(t("error.no_api_key"))
    return Json2Backend(url, api_key, args.db or config.database or guess_database(url))


def _print_test(backend: Json2Backend) -> None:
    info = check_connection(backend)
    print(t("cli.connection_ok"))
    print(f"  {t('cli.version')}: {info.version}")
    print(f"  {t('cli.user')}: {info.user_name} <{info.user_login}>")
    for company in info.companies:
        print(f"  {t('cli.company')}: {company.name} ({company.vat})")
    print(f"  {t('cli.invoices_total')}: {info.invoice_count}")


def _print_extract(backend: Json2Backend, args: argparse.Namespace, config: AppConfig) -> None:
    params = ExtractParams(args.year, None, args.project, args.supplier, args.include_receipts)
    dossier = extract_dossier(backend, params, config.payment_form_overrides, progress=print)
    print(f"{t('cli.company')}: {dossier.company.name}")
    print(f"{t('cli.invoices')}: {len(dossier.invoices)}")
    print(f"{t('cli.untaxed')}: {format_amount(dossier.total_untaxed)} €")
    print(f"{t('cli.tax')}: {format_amount(dossier.total_tax)} €")
    print(f"{t('cli.total')}: {format_amount(dossier.total)} €")
    print(f"{t('cli.paid')}: {format_amount(dossier.total_paid)} €")
    print(f"{t('cli.notices')}:" if dossier.notices else f"{t('cli.notices')}: {t('cli.none')}")
    for code, count in Counter(n.code for n in dossier.notices).items():
        print(f"  {count} x {code}")
    if args.json:
        args.json.write_text(
            json.dumps(asdict(dossier), ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        print(f"{t('cli.saved')} {args.json}")


def _print_excel(backend: Json2Backend, args: argparse.Namespace, config: AppConfig) -> None:
    params = ExtractParams(args.year, None, args.project, args.supplier, args.include_receipts)
    dossier = extract_dossier(backend, params, config.payment_form_overrides, progress=print)
    path = new_output_folder(args.out, args.year) / excel_filename(args.year)
    build_workbook(dossier, path)
    print(f"{t('cli.saved')} {path}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    config = AppConfig.load()
    try:
        backend = _backend(args, config)
        if args.command == "test":
            _print_test(backend)
        elif args.command == "years":
            print("\n".join(str(y) for y in list_years(backend)))
        elif args.command == "projects":
            for account in list_analytic_accounts(backend):
                print(f"{account['id']}\t{account['name']}\t{account['plan']}")
        elif args.command == "suppliers":
            for supplier in list_suppliers(backend, args.year):
                print(f"{supplier['id']}\t{supplier['name']}")
        elif args.command == "extract":
            _print_extract(backend, args, config)
        elif args.command == "excel":
            _print_excel(backend, args, config)
    except OdooError as error:
        print(t(error.i18n_key), file=sys.stderr)
        if args.verbose and error.detail:
            print(error.detail, file=sys.stderr)
        return 2
    return 0
