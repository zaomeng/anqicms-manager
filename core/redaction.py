"""Secret scrubbing for any text that may reach logs, UI or exceptions."""

from __future__ import annotations

import re
import threading
from typing import Iterable
from urllib.parse import quote, quote_plus

from core.site_credentials import import_token

MASK = "***"
MIN_SECRET_LENGTH = 4

_SENSITIVE_KEYS = (
    r"token|api[_-]?key|apikey|access[_-]?token|mcp[_-]?token|ticket|"
    r"password|passwd|admin[_-]?password|secret"
)
_QUERY_PATTERN = re.compile(
    rf"(?i)((?<![A-Za-z0-9])(?:{_SENSITIVE_KEYS})=)[^&\s\"'#]+"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=\-]+")
_JSON_PATTERN = re.compile(
    rf'(?i)("(?:{_SENSITIVE_KEYS})"\s*:\s*")((?:\\.|[^"\\])*)(")'
)
_USERINFO_PATTERN = re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://)[^/@\s]+@")


def _secret_variants(secret: str) -> set[str]:
    return {secret, quote_plus(secret), quote(secret, safe="")}


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask known secrets and common credential patterns in ``text``."""
    result = "" if text is None else str(text)
    if not result:
        return result
    variants: set[str] = set()
    for secret in secrets or ():
        if not isinstance(secret, str):
            continue
        cleaned = secret.strip()
        if len(cleaned) >= MIN_SECRET_LENGTH:
            variants.update(_secret_variants(cleaned))
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            result = result.replace(variant, MASK)
    result = _QUERY_PATTERN.sub(rf"\1{MASK}", result)
    result = _BEARER_PATTERN.sub(rf"\1{MASK}", result)
    result = _JSON_PATTERN.sub(rf"\1{MASK}\3", result)
    result = _USERINFO_PATTERN.sub(rf"\1{MASK}@", result)
    return result


class SecretRedactor:
    """Callable accumulating secrets: ``redactor(text) -> redacted``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self.add(*secrets)

    @classmethod
    def from_sites(cls, sites) -> "SecretRedactor":
        redactor = cls()
        for site in sites or ():
            redactor.add(import_token(site))
            for attribute in ("mcp_token", "admin_password"):
                value = getattr(site, attribute, "")
                if isinstance(value, str):
                    redactor.add(value)
        return redactor

    def add(self, *secrets: str) -> None:
        with self._lock:
            for secret in secrets:
                if isinstance(secret, str):
                    cleaned = secret.strip()
                    if len(cleaned) >= MIN_SECRET_LENGTH:
                        self._secrets.add(cleaned)

    def __call__(self, text: str) -> str:
        with self._lock:
            secrets = tuple(self._secrets)
        return redact_text(text, secrets)

    def __repr__(self) -> str:  # never reveal the secrets
        return f"SecretRedactor(secrets={len(self._secrets)})"
