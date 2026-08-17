"""/api/v1 aggregator — Phase 0 property, frozen.

Every module is already wired; Phase 1 agents fill in their own module
and never touch this file (AGENTS.md).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, bookmarks, feed, health, items, me, sources

router = APIRouter()
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(me.router)
router.include_router(sources.router)
router.include_router(feed.router)
router.include_router(items.router)
router.include_router(bookmarks.router)
