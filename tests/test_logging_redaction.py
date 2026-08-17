"""The redaction processor keeps secrets out of log output."""

from __future__ import annotations

from typing import Any

from app.logging import REDACTED, redact_sensitive


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
