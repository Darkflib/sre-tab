"""OAuth ``state``: signed, expiring, and single-use.

The token is ``nonce.expires_at.signature`` with the signature an
HMAC-SHA256 over ``nonce.expires_at`` under ``SESSION_SECRET``. Signing and
the embedded expiry are stateless checks; **single use** needs state, so
issued nonces are held in an in-process store and popped on first
redemption. That is sound for the v1 single-instance deployment — the same
constraint the rate limiter is written under (AGENTS.md) — and is where a
shared store would go if the service is ever replicated.

The route additionally binds the token to the browser with a short-lived
``HttpOnly`` cookie, so a state minted in one browser cannot be redeemed in
another (login CSRF).
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time

from app.security.tokens import compare_secret

_SEPARATOR = "."
DEFAULT_TTL_SECONDS = 600
STATE_COOKIE_NAME = "oauth_state"
STATE_COOKIE_PATH = "/api/v1/auth"

# Bound on outstanding nonces. The OAuth-start rate limiter is the primary
# defence; this is the backstop that keeps a burst from growing the process
# without limit.
_MAX_OUTSTANDING = 4096


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), "sha256").hexdigest()


class StateStore:
    """Issue and redeem single-use OAuth state tokens."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._outstanding: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, secret: str) -> str:
        nonce = secrets.token_urlsafe(16)
        expires_at = int(time.time()) + self._ttl
        payload = f"{nonce}{_SEPARATOR}{expires_at}"
        with self._lock:
            self._purge_locked()
            if len(self._outstanding) >= _MAX_OUTSTANDING:
                # Drop the oldest rather than refuse service.
                oldest = min(self._outstanding, key=lambda key: self._outstanding[key])
                del self._outstanding[oldest]
            self._outstanding[nonce] = expires_at
        return f"{payload}{_SEPARATOR}{_sign(payload, secret)}"

    def consume(self, token: str, secret: str) -> bool:
        """Redeem a token. False for a forged, expired, or replayed one."""
        parts = token.split(_SEPARATOR)
        if len(parts) != 3:
            return False
        nonce, expires_raw, signature = parts
        # compare_secret, not hmac.compare_digest: `signature` is a slice
        # of a caller-supplied token, and compare_digest raises on
        # non-ASCII rather than returning False. `consume` promises a
        # bool for *any* string, forged ones included.
        if not compare_secret(signature, _sign(f"{nonce}{_SEPARATOR}{expires_raw}", secret)):
            return False
        try:
            expires_at = int(expires_raw)
        except ValueError:
            return False
        if expires_at <= time.time():
            return False
        with self._lock:
            self._purge_locked()
            # Popping is what makes redemption single-use: a replay finds
            # nothing to pop, even though the signature still verifies.
            return self._outstanding.pop(nonce, None) is not None

    def reset(self) -> None:
        with self._lock:
            self._outstanding.clear()

    def _purge_locked(self) -> None:
        now = time.time()
        for nonce in [n for n, expiry in self._outstanding.items() if expiry <= now]:
            del self._outstanding[nonce]


# Process-wide store; the flow module and tests share this instance.
state_store = StateStore()
