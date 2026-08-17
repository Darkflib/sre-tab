"""OAuth ``state``: signed, expiring, single-use."""

from __future__ import annotations

import time_machine

from app.auth.state import StateStore

SECRET = "state-secret"


def test_issued_state_redeems_once() -> None:
    store = StateStore()
    token = store.issue(SECRET)
    assert store.consume(token, SECRET) is True
    # Replay: the signature still verifies, but the nonce is gone.
    assert store.consume(token, SECRET) is False


def test_expired_state_is_rejected() -> None:
    store = StateStore(ttl_seconds=60)
    with time_machine.travel("2026-08-17 09:00:00 +0000", tick=False) as traveller:
        token = store.issue(SECRET)
        traveller.shift(61)
        assert store.consume(token, SECRET) is False


def test_state_within_ttl_is_accepted() -> None:
    store = StateStore(ttl_seconds=60)
    with time_machine.travel("2026-08-17 09:00:00 +0000", tick=False) as traveller:
        token = store.issue(SECRET)
        traveller.shift(59)
        assert store.consume(token, SECRET) is True


def test_forged_and_malformed_state_is_rejected() -> None:
    store = StateStore()
    token = store.issue(SECRET)
    nonce, expires_at, signature = token.split(".")

    assert store.consume(token, "another-secret") is False
    # Flip the last hex digit to something it demonstrably is not. Hard-coding
    # a digit here is a 1-in-16 flake: when the real signature already ends in
    # it, the "tampered" token is the genuine one and redemption rightly
    # succeeds.
    tampered = signature[:-1] + ("1" if signature[-1] == "0" else "0")
    assert store.consume(f"{nonce}.{expires_at}.{tampered}", SECRET) is False
    # Extending the deadline invalidates the signature over it.
    assert store.consume(f"{nonce}.{int(expires_at) + 3600}.{signature}", SECRET) is False
    assert store.consume("nonsense", SECRET) is False
    assert store.consume("a.b.c", SECRET) is False
    # None of the above burned the nonce, so the genuine token still works.
    assert store.consume(token, SECRET) is True


def test_unknown_nonce_with_a_valid_signature_is_rejected() -> None:
    """Signature verification alone is not enough: a token issued by a
    different store instance (a restarted process, or another replica)
    carries no redeemable nonce here."""
    token = StateStore().issue(SECRET)
    assert StateStore().consume(token, SECRET) is False


def test_reset_clears_outstanding_nonces() -> None:
    store = StateStore()
    token = store.issue(SECRET)
    store.reset()
    assert store.consume(token, SECRET) is False
