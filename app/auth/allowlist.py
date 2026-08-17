"""Authorisation seam.

v1 authorisation is a static allow-list of GitHub numeric user IDs read
from ``ALLOWED_GITHUB_IDS`` (PLAN-v1.md, decision 1). It is deliberately a
single function so v2 can swap in organisation or team resolution without
disturbing the OAuth flow around it.

**Fail closed.** The tempting shape is::

    if allowed and github_id not in allowed:   # WRONG
        deny()

which reads as "restrict when configured" and silently admits the entire
internet when the variable is unset or blank. An unconfigured instance is
an instance nobody may sign in to.
"""

from __future__ import annotations

from app.settings import Settings


def is_authorised(github_id: int, settings: Settings) -> bool:
    """True only if this GitHub numeric ID is explicitly allow-listed."""
    allowed = settings.allowed_github_ids
    if not allowed:
        return False
    return github_id in allowed
