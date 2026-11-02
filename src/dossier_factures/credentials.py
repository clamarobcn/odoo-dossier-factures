"""API key storage in the operating system's credential vault."""

import logging
import os

import keyring
from keyring.errors import KeyringError

from .config import APP_NAME

logger = logging.getLogger(__name__)

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


def set_api_key(base_url: str, user: str, api_key: str) -> bool:
    """Save the key; return False if the vault is not available."""
    try:
        keyring.set_password(APP_NAME, _account(base_url, user), api_key)
    except KeyringError:
        logger.warning("The credential vault is not available")
        return False
    return True


def delete_api_key(base_url: str, user: str) -> None:
    try:
        keyring.delete_password(APP_NAME, _account(base_url, user))
    except KeyringError:
        logger.info("No saved key to delete")
