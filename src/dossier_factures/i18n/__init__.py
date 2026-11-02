"""Interface texts. Catalan is the default; add ``<lang>.json`` to support another language."""

import json
from importlib import resources

DEFAULT_LANGUAGE = "ca"

_language = DEFAULT_LANGUAGE
_catalogs: dict[str, dict[str, str]] = {}


def _catalog(language: str) -> dict[str, str]:
    if language not in _catalogs:
        text = resources.files(__name__).joinpath(f"{language}.json").read_text(encoding="utf-8")
        _catalogs[language] = json.loads(text)
    return _catalogs[language]


def set_language(language: str) -> None:
    global _language
    _catalog(language)
    _language = language


def t(key: str, **params: object) -> str:
    """Translate a key, falling back to the default language and then to the key itself."""
    text = _catalog(_language).get(key) or _catalog(DEFAULT_LANGUAGE).get(key) or key
    return text.format(**params) if params else text
