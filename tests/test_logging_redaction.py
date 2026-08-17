"""The redaction processor keeps secrets out of log output."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

from app.logging import REDACTED, configure_logging, redact_sensitive
from app.settings import Settings


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    return dict(redact_sensitive(None, "info", event))


def test_sensitive_keys_redacted() -> None:
    event = _redact(
        {
            "event": "oauth_callback",
            "code": "gho_code_value",
            "access_token": "tok",
            "cookie": "session=abc",
            "preferences": {"theme": "dark"},
            "github_token": "x",
        }
    )
    for key in ("code", "access_token", "cookie", "preferences", "github_token"):
        assert event[key] == REDACTED


def test_nested_and_listed_payloads_redacted() -> None:
    event = _redact({"payload": {"client_secret": "s", "items": [{"session_token": "t"}]}})
    assert event["payload"]["client_secret"] == REDACTED
    assert event["payload"]["items"][0]["session_token"] == REDACTED


def test_token_shaped_values_scrubbed_inside_strings() -> None:
    event = _redact(
        {
            "url": "https://example.org/cb?code=abc123&state=xyz",
            "header": "Bearer abc.def.ghi",
            "note": "leaked ghp_ABCDEFghijklmnop1234 in text",
        }
    )
    assert "abc123" not in event["url"]
    assert "state=xyz" in event["url"]
    assert event["header"] == REDACTED
    assert "ghp_" not in event["note"]


def test_benign_keys_untouched() -> None:
    event = _redact({"status_code": 200, "path": "/api/v1/feed", "duration_ms": 1.2})
    assert event == {"status_code": 200, "path": "/api/v1/feed", "duration_ms": 1.2}


# --- the configured chain, not just the processor ------------------------

OAUTH_CODE = "oauth-code-must-never-be-logged"
CLIENT_SECRET = "client-secret-must-never-be-logged"


@pytest.fixture
def configured_logging() -> Iterator[None]:
    """Install the real production chain, then put structlog back.

    Asserting against ``redact_sensitive`` alone cannot see an ordering
    defect; only the assembled chain can.
    """
    configure_logging(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            database_url="sqlite://",
            session_secret=SecretStr("test-session-secret"),
            log_json=True,
        )
    )
    yield
    structlog.reset_defaults()


def _raise_holding_secrets() -> None:
    """A frame whose locals are exactly what the PRD forbids logging.

    Modelled on ``exchange_code`` and ``complete_sign_in``, where ``code``
    and ``client_secret`` really are live locals.
    """
    code = OAUTH_CODE  # noqa: F841 - an unused local is the whole point
    client_secret = CLIENT_SECRET  # noqa: F841
    raise RuntimeError("upstream refused")


def test_frame_locals_never_reach_the_log(
    configured_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """structlog's ExceptionDictTransformer defaults to show_locals=True,
    which writes every frame's locals into exception[].frames[].locals."""
    log = structlog.get_logger("tests.exception_locals")
    try:
        _raise_holding_secrets()
    except RuntimeError:
        log.exception("ingest_failed", code=OAUTH_CODE)

    captured = capsys.readouterr().out
    # The traceback is still rendered — this must not pass by logging less.
    assert "ingest_failed" in captured
    assert "RuntimeError" in captured
    assert OAUTH_CODE not in captured
    assert CLIENT_SECRET not in captured
    assert REDACTED in captured


def test_redaction_reaches_inside_the_rendered_traceback(
    configured_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Traceback rendering manufactures keys the redactor must still see,
    which it cannot if it runs first."""
    log = structlog.get_logger("tests.exception_message")
    try:
        raise RuntimeError(f"upstream rejected code={OAUTH_CODE}")
    except RuntimeError:
        log.exception("ingest_failed")

    captured = capsys.readouterr().out
    assert "ingest_failed" in captured
    assert OAUTH_CODE not in captured
    assert REDACTED in captured
