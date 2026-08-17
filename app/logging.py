"""Structured logging: structlog JSON output, request IDs, redaction.

The redaction processor is a backstop, not a licence — code must still
never pass OAuth codes, access tokens, cookie values, or full preference
payloads to the logger (PRD, Technical requirements).

Redaction is the *last* processor before rendering, and that position is
load-bearing rather than incidental: traceback rendering manufactures new
keys out of exception state, so a redactor placed ahead of it inspects an
event that does not yet contain the material it exists to remove.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from starlette.datastructures import Headers, MutableHeaders

from app.settings import Settings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

REDACTED = "[redacted]"

# Exact key matches (lowered). "code" covers OAuth authorization codes;
# "preferences" covers full preference payloads.
_SENSITIVE_KEYS = frozenset(
    {
        "code",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "session_secret",
        "secret",
        "password",
        "authorization",
        "cookie",
        "cookies",
        "set-cookie",
        "set_cookie",
        "csrf_token",
        "session_token",
        "preferences",
        "preference_payload",
    }
)
_SENSITIVE_SUFFIXES = ("_token", "_secret", "_password", "_cookie")

# Token-shaped material inside string values: GitHub token prefixes,
# bearer credentials, and code/token query parameters in URLs.
_VALUE_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(code|access_token|refresh_token|client_secret)=[^&\s]+"),
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or lowered.endswith(_SENSITIVE_SUFFIXES)


def _scrub_value(value: object) -> object:
    if isinstance(value, str):
        for pattern in _VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return {
            k: REDACTED if _is_sensitive_key(str(k)) else _scrub_value(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item) for item in value]
    return value


def redact_sensitive(
    logger: structlog.typing.WrappedLogger,
    method_name: str,
    event_dict: structlog.typing.EventDict,
) -> structlog.typing.EventDict:
    """structlog processor: redact sensitive keys and token-shaped values."""
    for key in list(event_dict):
        if _is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _scrub_value(event_dict[key])
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    renderer: structlog.typing.Processor
    if settings.log_json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            # Two deliberate departures from ``structlog.processors.dict_tracebacks``:
            #
            # ``show_locals=False``. That default is True, and it writes
            # every frame's local variables into the event. ``code`` is a
            # live local in ``github_callback`` and ``complete_sign_in``
            # and ``client_secret`` in ``exchange_code``, so the first
            # ``log.exception`` on the OAuth path would put both in
            # cleartext — which the PRD forbids outright.
            #
            # *Order.* Redaction runs after this, not before. Traceback
            # rendering produces new keys out of exception state, and a
            # redactor placed ahead of it never sees them: reproduced with
            # the event key ``code`` correctly redacted while the same
            # value sat verbatim in ``exception[].frames[].locals``.
            structlog.processors.ExceptionRenderer(
                structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
            ),
            redact_sensitive,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level, format="%(message)s")


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestIDMiddleware:
    """Bind a request ID into the structlog context and echo it back.

    Honours a well-formed incoming ``X-Request-ID`` (so a reverse proxy's
    ID is preserved) and generates one otherwise. Also emits one
    ``request_completed`` event per request — path only, never the query
    string, which may carry OAuth parameters.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_holder: dict[str, Any] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.get_logger("app.request").info(
                "request_completed",
                method=scope.get("method"),
                path=scope.get("path"),
                status=status_holder.get("status"),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            structlog.contextvars.clear_contextvars()
