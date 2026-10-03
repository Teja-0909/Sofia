"""Redact credentials at the logging output boundary.

Apply this after loading configuration and before starting clients. Every existing
handler is protected, including handlers on non-propagating library loggers. Code
that adds a handler later must call ``protect_handler`` for that handler as well.
No environment or credential file is read by this module.
"""
from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"

# Match credential *names*, rather than assuming one provider's token shape.
_CREDENTIAL_NAME = re.compile(r"(?:^|[_-])(?:token|secret|password|passwd|api[_-]?key)$", re.IGNORECASE)
_KEY = r"(?:[\w-]*(?:token|secret|password|passwd|api[_-]?key)|(?:proxy[-_]?)?authorization)"
_PATTERNS = (
    # Telegram uses credentials in both API and file-download paths. Relative
    # paths also occur in httpcore request/debug representations.
    (re.compile(r"(/(?:file/)?bot)[^/\s?#\"'<>\\]+", re.IGNORECASE), r"\1" + REDACTED),
    # Userinfo can contain a username and password (including encoded values).
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@\"'<>]+@", re.IGNORECASE), r"\1" + REDACTED + "@"),
    # Redact common header schemes even without an Authorization key.
    (re.compile(r"(\b(?:Bearer|Basic)\s+)(?:\[REDACTED\]|[^\s,;\"'\]\)}>]+)", re.IGNORECASE), r"\1" + REDACTED),
    # Quoted mappings and httpcore's [(b'Header', b'value')] representations.
    (re.compile(r"(\b" + _KEY + r"[\"']?\s*[:=,]\s*b?)([\"'])((?:\\[\s\S]|(?!\2)[^\\])*)(\2)", re.IGNORECASE),
     r"\1\2" + REDACTED + r"\4"),
    # Unquoted Authorization headers may contain spaces (e.g. Digest).
    (re.compile(r"(\b(?:proxy[-_]?)?authorization\s*[:=]\s*)(?!\s|b?[\"'])[^\r\n]+", re.IGNORECASE),
     r"\1" + REDACTED),
    # URL queries, including Google's ?key= API credential convention.
    (re.compile(r"([?&](?:" + _KEY + r"|key|auth|signature)=)[^&#\s\"'<>]+", re.IGNORECASE),
     r"\1" + REDACTED),
    # Ordinary key=value / key: value diagnostics.
    (re.compile(r"(\b" + _KEY + r"\s*[:=]\s*)(?:\[REDACTED\]|[^\s&;,\"'\]\)}>]+)", re.IGNORECASE),
     r"\1" + REDACTED),
)


class CredentialRedactor:
    """Redact known config credentials plus common unknown credential formats."""

    def __init__(self, config_values: Mapping[str, object] | None = None) -> None:
        secrets = set()
        for name, value in (config_values or {}).items():
            if _CREDENTIAL_NAME.search(name) and isinstance(value, str) and value:
                # Libraries can log raw values, URL encoding, or repr escapes.
                secrets.update((value, quote(value, safe=""), quote_plus(value),
                                repr(value)[1:-1], str(value.encode())[2:-1]))
        self._secrets = re.compile("|".join(re.escape(value) for value in sorted(
            secrets, key=len, reverse=True,
        ))) if secrets else None

    def redact(self, text: str) -> str:
        # Exact values go first: a generic pattern must not remove only the
        # first part of a configured secret containing spaces or punctuation.
        if self._secrets is not None:
            text = self._secrets.sub(REDACTED, text)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


class RedactingFilter(logging.Filter):
    """Keep rendered messages safe on the shared record, too (e.g. capture)."""

    def __init__(self, redactor: CredentialRedactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.redact(record.getMessage())
        record.args = ()
        if record.exc_text:
            record.exc_text = self.redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redactor.redact(record.stack_info)
        return True


class RedactingFormatter(logging.Formatter):
    """Sanitize the final output, including exceptions, stacks and extra fields."""

    def __init__(self, formatter: logging.Formatter, redactor: CredentialRedactor) -> None:
        super().__init__()
        self.formatter = formatter
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        # Formatter.format caches exception text on records. Isolate that cache
        # so neither raw exception text nor another formatter's layout escapes.
        safe_record = copy.copy(record)
        if safe_record.exc_info:
            safe_record.exc_text = None
        return self.redactor.redact(self.formatter.format(safe_record))


def protect_handler(handler: logging.Handler, redactor: CredentialRedactor) -> None:
    """Install/update idempotent protection without replacing a handler's layout."""
    formatter = handler.formatter or logging.Formatter()
    if isinstance(formatter, RedactingFormatter):
        formatter = formatter.formatter
    handler.setFormatter(RedactingFormatter(formatter, redactor))
    for log_filter in list(handler.filters):
        if isinstance(log_filter, RedactingFilter):
            handler.removeFilter(log_filter)
    handler.addFilter(RedactingFilter(redactor))


def configure_safe_logging(config_values: Mapping[str, object], level: int = logging.INFO) -> None:
    """Protect root and existing library handlers before starting the service."""
    logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=level)
    root = logging.getLogger()
    root.setLevel(level)
    install_log_redaction(config_values)


def install_log_redaction(config_values: Mapping[str, object] | None = None) -> None:
    """Protect existing handlers, preserving logger levels and output layout."""
    root = logging.getLogger()
    redactor = CredentialRedactor(config_values)
    handlers = set(root.handlers)
    for logger in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(logger, logging.Logger):
            handlers.update(logger.handlers)
    if logging.lastResort:
        handlers.add(logging.lastResort)
    for handler in handlers:
        protect_handler(handler, redactor)
