"""Exceptions raised by Odoo backends.

Each error carries an i18n key, so the interface can show a message a non-technical
person understands. Server tracebacks are never stored here.
"""


class OdooError(Exception):
    """Base class for every error that comes from talking to Odoo."""

    i18n_key = "error.unknown"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.i18n_key)
        self.detail = detail


class ReadOnlyViolation(OdooError):
    """A method outside the read-only whitelist was requested. Nothing was sent to Odoo."""

    i18n_key = "error.read_only_violation"

    def __init__(self, method: object) -> None:
        super().__init__(str(method))
        self.method = method


class InvalidUrlError(OdooError):
    i18n_key = "error.invalid_url"


class InsecureUrlError(OdooError):
    i18n_key = "error.insecure_url"


class InvalidApiKeyError(OdooError):
    i18n_key = "error.invalid_api_key"


class AccessOrPlanError(OdooError):
    i18n_key = "error.access_or_plan"


class ApiNotFoundError(OdooError):
    i18n_key = "error.api_not_found"


class UnexpectedRedirectError(OdooError):
    i18n_key = "error.unexpected_redirect"


class ServerBusyError(OdooError):
    i18n_key = "error.server_busy"


class NetworkError(OdooError):
    i18n_key = "error.network"


class OdooApiError(OdooError):
    i18n_key = "error.api"


class MissingFieldsError(OdooError):
    """The server lacks fields the application needs, usually after an Odoo upgrade."""

    i18n_key = "error.missing_fields"

    def __init__(self, missing: dict[str, list[str]]) -> None:
        detail = "; ".join(f"{model}: {', '.join(fields)}" for model, fields in missing.items())
        super().__init__(detail)
        self.missing = missing


class AttachmentIntegrityError(OdooError):
    i18n_key = "error.attachment_integrity"
