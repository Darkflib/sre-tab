"""Credential primitives — stdlib only, by design.

Two kinds of credential are minted here and both are stored the same way.
A *session* token lives solely in the session cookie; an *API* token is
shown to its owner exactly once, at creation. The database stores the
SHA-256 hex digest in both cases (``sessions.token_hash`` and
``api_tokens.token_hash``), so a leaked database yields no usable
credential. No per-token salt is needed: tokens are 256-bit random
values, so rainbow tables do not apply.

The two differ in exactly one respect, and it is deliberate. An API token
carries a fixed, human-visible prefix; a session token does not. A
session token is read by one program, ours, out of a cookie it set. An
API token is pasted into other people's configuration files, shell
history, and CI variables, so the useful property is being *recognisable
out of context* — greppable with a string nobody types by accident, and
matchable by a secret scanner, which keys on exactly this kind of literal
prefix. That costs nothing: the prefix is not part of the entropy, and it
is not claimed to be.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: Bytes of randomness in every token this module mints. 32 bytes is 256
#: bits, which ``secrets.token_urlsafe`` renders as 43 base64url
#: characters. Guessing is not a threat model at that size; the credential
#: is protected by not being disclosed, not by being rate limited.
_TOKEN_BYTES = 32

#: The fixed, greppable prefix on every API token. Chosen to be a string
#: that appears nowhere else — ``sretab_`` alone would collide with
#: ordinary identifiers, and half the value here is that a match is a
#: finding rather than a false positive.
#:
#: The ``nosec`` is the first in this repository and is narrow on purpose.
#: Bandit's B105 fires on any string literal assigned to a name containing
#: "token", which is the correct heuristic and the wrong answer here: this
#: literal is the *public* half of the credential, present verbatim in the
#: README, in every request that carries a token, and — deliberately — in
#: any secret scanner's ruleset. Suppressed rather than renamed around,
#: because ``API_CREDENTIAL_PREFIX`` would be a worse name chosen to
#: please a linter.
API_TOKEN_PREFIX = "sretab_pat_"  # nosec B105 - a public prefix, not a secret

#: Characters of the random part kept alongside the prefix as a
#: non-secret display prefix, so a user can tell two tokens apart in a
#: list. Six base64url characters is 36 bits, leaving 220 of the 256
#: unrevealed — a margin that is not close to mattering, and small enough
#: that two of a user's own tokens practically never collide.
API_TOKEN_DISPLAY_CHARS = 6


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def generate_session_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    return _sha256_hex(token)


def generate_api_token() -> str:
    """A long-lived API token: the fixed prefix, then 256 bits of entropy."""
    return f"{API_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"


def hash_api_token(token: str) -> str:
    """The digest stored in ``api_tokens.token_hash``.

    The whole token including the prefix is hashed, not the random part
    alone. The prefix is not a secret and adds nothing to the digest's
    strength; hashing it anyway means the stored value is a function of
    the string the client actually presents, so there is no step between
    "what arrived" and "what we look up" for a bug to hide in.
    """
    return _sha256_hex(token)


def api_token_display_prefix(token: str) -> str:
    """The non-secret leading slice kept on the row.

    Derived from the token rather than generated separately, so it cannot
    drift from the token it labels. It is stored in cleartext on purpose:
    it is a label, and the security claim is only that what it reveals is
    negligible — see :data:`API_TOKEN_DISPLAY_CHARS`.
    """
    return token[: len(API_TOKEN_PREFIX) + API_TOKEN_DISPLAY_CHARS]


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
