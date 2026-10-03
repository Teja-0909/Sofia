"""Logging regressions use fabricated credentials and offline transports only."""
import io
import logging
import sys
from unittest.mock import patch
from urllib.parse import quote, quote_plus

import httpx
import pytest

from app.safe_logging import (
    REDACTED,
    CredentialRedactor,
    RedactingFilter,
    RedactingFormatter,
    configure_safe_logging,
    install_log_redaction,
    protect_handler,
)

FAKE_BOT_TOKEN = "123456789:offline-telegram-sentinel"


@pytest.fixture
def restore_logging():
    """The installer is process-wide; don't change other tests' capture setup."""
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    handlers = set(root_handlers)
    for logger in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(logger, logging.Logger):
            handlers.update(logger.handlers)
    if logging.lastResort:
        handlers.add(logging.lastResort)
    saved = {handler: (handler.formatter, list(handler.filters)) for handler in handlers}
    yield
    root.handlers[:] = root_handlers
    root.setLevel(root_level)
    for handler, (formatter, filters) in saved.items():
        handler.setFormatter(formatter)
        handler.filters[:] = filters


@pytest.mark.parametrize("message, forbidden, preserved", [
    (f'POST https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getUpdates "HTTP/1.1 200 OK"',
     FAKE_BOT_TOKEN, '/getUpdates "HTTP/1.1 200 OK"'),
    (f"https://api.telegram.org/file/bot{FAKE_BOT_TOKEN}/photos/test.jpg",
     FAKE_BOT_TOKEN, "/photos/test.jpg"),
    (f"request=POST path=b'/bot{quote(FAKE_BOT_TOKEN, safe='')}/sendMessage'",
     "offline-telegram-sentinel", "/sendMessage"),
    ("Authorization: Bearer offline-auth-sentinel", "offline-auth-sentinel", "Authorization:"),
    ("authorization: Basic offline-basic-sentinel", "offline-basic-sentinel", "authorization:"),
    ("Authorization: Digest username=\"offline-user\", response=\"offline-digest\"",
     "offline-digest", "Authorization:"),
    ("{'Authorization': 'Bearer offline-auth-sentinel', 'status': 401}",
     "offline-auth-sentinel", "'status': 401"),
    ("headers=[(b'authorization', b'Bearer offline-header-sentinel')]",
     "offline-header-sentinel", "headers="),
    ("headers=[(b'X-Auth-Token', b'offline-web-sentinel')]",
     "offline-web-sentinel", "X-Auth-Token"),
    ("X-Auth-Token: offline-web-sentinel", "offline-web-sentinel", "X-Auth-Token:"),
    ("request rejected: Bearer offline-bearer-sentinel", "offline-bearer-sentinel", "request rejected:"),
    ("API_KEY=offline-api-sentinel status=503", "offline-api-sentinel", "status=503"),
    ("{'password': 'offline password sentinel', 'status': 503}",
     "offline password sentinel", "'status': 503"),
    ("https://test.invalid/generate?key=offline-query-sentinel&model=test-model",
     "offline-query-sentinel", "model=test-model"),
    ("https://test.invalid/?access_token=offline-access-sentinel&status=503",
     "offline-access-sentinel", "status=503"),
    ("https://test.invalid/?api_key=offline-api-sentinel&status=503",
     "offline-api-sentinel", "status=503"),
    ("https://offline-user:offline-password@test.invalid/request status=503",
     "offline-password", "test.invalid/request status=503"),
])
def test_unknown_credentials_are_redacted(message, forbidden, preserved):
    redactor = CredentialRedactor()
    output = redactor.redact(message)
    assert forbidden not in output
    assert preserved in output
    assert REDACTED in output
    assert redactor.redact(output) == output


def test_known_config_credentials_raw_encoded_and_escaped():
    secret = "offline config 'sentinel' / +:\nend"
    redactor = CredentialRedactor({"GEMINI_API_KEY": secret, "BOT_TOKEN": "", "PORT": 10000})
    variants = (secret, quote(secret, safe=""), quote_plus(secret), repr(secret)[1:-1])
    for value in variants:
        output = redactor.redact(f"request failed using {value}; status=503")
        assert value not in output
        assert "sentinel" not in output
        assert "status=503" in output
    assert redactor.redact(f"api_key={secret} status=503") == f"api_key={REDACTED} status=503"


def test_noncredentials_and_empty_values_do_not_hide_safe_diagnostics():
    message = "HTTP Request: GET https://example.invalid/ready HTTP/1.1 503 Service Unavailable"
    redactor = CredentialRedactor({"BOT_TOKEN": "", "MODEL": "HTTP", "PORT": 503, "MAX_TOKENS": "503"})
    assert redactor.redact(message) == message


@pytest.mark.parametrize("args", [(FAKE_BOT_TOKEN,), {"token": FAKE_BOT_TOKEN}])
def test_filter_renders_positional_and_mapping_arguments(args):
    message = "failed credential=%s" if isinstance(args, tuple) else "failed credential=%(token)s"
    # LogRecord expects mapping arguments inside a one-item tuple.
    record_args = args if isinstance(args, tuple) else (args,)
    record = logging.LogRecord("app.test", logging.WARNING, __file__, 1, message, record_args, None)
    redactor = CredentialRedactor({"BOT_TOKEN": FAKE_BOT_TOKEN})
    assert RedactingFilter(redactor).filter(record)
    assert FAKE_BOT_TOKEN not in record.getMessage()
    assert record.args == ()


def test_formatter_covers_exception_chain_stack_extra_and_cached_text():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s request=%(request_url)s"))
    protect_handler(handler, CredentialRedactor({"BOT_TOKEN": FAKE_BOT_TOKEN}))
    logger = logging.getLogger("app.sentinel")
    url = f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getUpdates"
    try:
        try:
            raise ValueError(f"Rejected upstream request {url}")
        except ValueError as cause:
            raise RuntimeError("Safe context: startup failed") from cause
    except RuntimeError:
        record = logger.makeRecord("app.sentinel", logging.ERROR, __file__, 1,
                                   "Polling failed", (), sys.exc_info(),
                                   extra={"request_url": url}, sinfo=f"Stack info: {url}")
    record.exc_text = f"Previously cached unsafe exception {url}"
    handler.handle(record)
    output = stream.getvalue()
    assert FAKE_BOT_TOKEN not in output
    assert "Traceback" in output
    assert "ValueError: Rejected upstream request" in output
    assert "RuntimeError: Safe context: startup failed" in output
    assert "Stack info:" in output
    assert "/getUpdates" in output
    assert FAKE_BOT_TOKEN not in record.exc_text


def test_existing_nonpropagating_handler_and_custom_format_are_protected(restore_logging):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("CUSTOM %(levelname)s %(message)s"))
    logger = logging.getLogger("httpcore.sentinel")
    with patch.object(logger, "handlers", [handler]), patch.object(logger, "propagate", False), \
         patch.object(logger, "level", logging.DEBUG):
        install_log_redaction({"WEB_AUTH_TOKEN": "offline-web-sentinel"})
        logger.debug("headers=%s", [(b"X-Auth-Token", b"offline-web-sentinel")])
    output = stream.getvalue()
    assert output.startswith("CUSTOM DEBUG headers=")
    assert "offline-web-sentinel" not in output
    assert REDACTED in output


def test_httpx_real_request_logging_and_caplog_are_redacted(caplog, restore_logging):
    caplog.set_level(logging.INFO, logger="httpx")
    install_log_redaction({"BOT_TOKEN": FAKE_BOT_TOKEN})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport, trust_env=False) as client:
        response = client.post(f"https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getUpdates")
    assert response.status_code == 200
    assert "httpx" in caplog.text
    assert "/getUpdates" in caplog.text
    assert "200 OK" in caplog.text
    assert FAKE_BOT_TOKEN not in caplog.text
    assert all(FAKE_BOT_TOKEN not in record.getMessage() for record in caplog.records)


def test_fresh_child_logger_propagates_through_protected_handlers(caplog, restore_logging):
    configure_safe_logging({"GROQ_API_KEY": "offline-groq-sentinel"})
    logger = logging.getLogger("app.new_after_configuration")
    logger.warning("Provider rejected %s: status=401", "offline-groq-sentinel")
    assert "offline-groq-sentinel" not in caplog.text
    assert "status=401" in caplog.text


def test_reinstallation_is_idempotent_and_preserves_layout(restore_logging):
    handler = logging.StreamHandler(io.StringIO())
    original = logging.Formatter("PREFIX %(message)s")
    handler.setFormatter(original)
    for token in ("offline-first-sentinel", "offline-second-sentinel"):
        protect_handler(handler, CredentialRedactor({"API_KEY": token}))
    assert isinstance(handler.formatter, RedactingFormatter)
    assert handler.formatter.formatter is original
    assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "offline-second-sentinel", (), None)
    handler.handle(record)
    assert handler.stream.getvalue() == f"PREFIX {REDACTED}\n"


def test_last_resort_handler_is_protected(restore_logging):
    install_log_redaction()
    assert isinstance(logging.lastResort.formatter, RedactingFormatter)


def test_main_logs_fatal_exceptions_safely_and_exits_nonzero(caplog, restore_logging):
    import run

    def fail_before_start(coro):
        coro.close()
        raise RuntimeError(f"request failed https://api.telegram.org/bot{FAKE_BOT_TOKEN}/getUpdates")

    with patch.object(run.config, "BOT_TOKEN", FAKE_BOT_TOKEN), \
         patch.object(run.config, "ALLOWED_USER_ID", 12345), \
         patch.object(run.asyncio, "run", side_effect=fail_before_start), \
         pytest.raises(SystemExit) as stopped:
        run.main()
    assert stopped.value.code == 1
    assert stopped.value.__suppress_context__
    assert "Sofia bot failed" in caplog.text
    assert "RuntimeError: request failed" in caplog.text
    assert FAKE_BOT_TOKEN not in caplog.text
