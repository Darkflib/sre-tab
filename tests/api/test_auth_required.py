"""No route in this area answers without a signed-in user.

Successor to Phase 0's ``tests/test_stub_endpoints.py``, which existed to
assert 501 for the unimplemented twelve and had nothing left to cover once
agents A and C replaced the stubs. The property worth keeping is the one
that outlives the stubs: every route here depends on ``get_current_user``,
so an anonymous caller never reaches the query.

The accepted statuses span the handover — ``get_current_user`` answers 501
until agent A supplies the body and 401 afterwards. What must never appear
is a 200.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/api/v1/sources", {}),
    ("GET", "/api/v1/feed", {}),
    ("PUT", "/api/v1/items/1/read-state", {"json": {"read": True}}),
    ("GET", "/api/v1/bookmarks", {}),
    ("PUT", "/api/v1/items/1/bookmark", {}),
    ("DELETE", "/api/v1/items/1/bookmark", {}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), ROUTES)
def test_anonymous_requests_are_refused(
    client: TestClient, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = client.request(method, path, **kwargs)

    assert response.status_code in {401, 403, 501}, f"{method} {path} -> {response.status_code}"
