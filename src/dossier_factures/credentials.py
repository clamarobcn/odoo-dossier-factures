"""API key storage in the operating system's credential vault."""

import os

import keyring
from keyring.errors import KeyringError

from .config import APP_NAME

ENV_VARIABLE = "ODOO_API_KEY"


def _account(base_url: str, user: str) -> str:
    return f"{base_url}|{user}"


def get_api_key(base_url: str, user: str) -> str:
    """Return the key from the environment or from the vault, or an empty string."""
    if key := os.environ.get(ENV_VARIABLE):
        return key
    try:
        return keyring.get_password(APP_NAME, _account(base_url, user)) or ""
    except KeyringError:
        return ""


def set_api_key(base_url: str, user: str, api_key: str) -> None:
    keyring.set_password(APP_NAME, _account(base_url, user), api_key)
