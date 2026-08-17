"""Service-layer errors.

Routes translate these to HTTP; the service layer never imports FastAPI.
``InvalidCursorError`` and ``UnknownSlugError`` derive from ``ValueError``
so a caller that only knows the frozen contract — agent A's ``me.py``
catches ``ValueError`` from :func:`app.services.preferences.apply_patch`
and returns 422 — keeps working.
"""

from __future__ import annotations


class InvalidCursorError(ValueError):
    """A pagination cursor was absent from our encoding, truncated, or
    tampered with. Routes map this to 400, never 500."""


class UnknownSlugError(ValueError):
    """A topic or source slug does not exist, or exists but is disabled."""


class ItemNotFoundError(LookupError):
    """No feed item with the requested id. Routes map this to 404."""
