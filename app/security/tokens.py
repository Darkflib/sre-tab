"""Session-token primitives — stdlib only, by design.

The raw token lives solely in the session cookie; the database stores the
SHA-256 hex digest (``sessions.token_hash``). A leaked database therefore
yields no usable session credentials. No per-token salt is needed: tokens
are 256-bit random values, so rainbow tables do not apply.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def compare_secret(left: str, right: str) -> bool:
    """Constant-time equality for two credential strings.

    ``hmac.compare_digest`` refuses a ``str`` carrying any non-ASCII
    character — it raises ``TypeError`` rather than returning ``False``.
    Every value compared through here arrives from a client, and uvicorn
    decodes request headers as latin-1, so one byte in 0x80-0xff is
    enough to turn an intended refusal into an unhandled 500. Comparing
    the encoded bytes keeps the timing property and makes the function
    total: it answers, it never raises.
    """
    return hmac.compare_digest(
        left.encode("utf-8", "surrogatepass"), right.encode("utf-8", "surrogatepass")
    )
