"""Session-token primitives — stdlib only, by design.

The raw token lives solely in the session cookie; the database stores the
SHA-256 hex digest (``sessions.token_hash``). A leaked database therefore
yields no usable session credentials. No per-token salt is needed: tokens
are 256-bit random values, so rainbow tables do not apply.
"""

from __future__ import annotations

import hashlib
import secrets


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
