"""Read-only clients for the Odoo external APIs."""

from .base import READ_ONLY_METHODS, OdooBackend, chunks, ensure_read_only
from .errors import (
    AccessOrPlanError,
    ApiNotFoundError,
    AttachmentIntegrityError,
    InsecureUrlError,
    InvalidApiKeyError,
    InvalidUrlError,
    MissingFieldsError,
    NetworkError,
    OdooApiError,
    OdooError,
    ReadOnlyViolation,
    ServerBusyError,
    UnexpectedRedirectError,
)
from .json2 import Json2Backend, database_from_url, normalize_base_url

__all__ = [
    "READ_ONLY_METHODS",
    "AccessOrPlanError",
    "ApiNotFoundError",
    "AttachmentIntegrityError",
    "InsecureUrlError",
    "InvalidApiKeyError",
    "InvalidUrlError",
    "Json2Backend",
    "MissingFieldsError",
    "NetworkError",
    "OdooApiError",
    "OdooBackend",
    "OdooError",
    "ReadOnlyViolation",
    "ServerBusyError",
    "UnexpectedRedirectError",
    "chunks",
    "database_from_url",
    "ensure_read_only",
    "normalize_base_url",
]
