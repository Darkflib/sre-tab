"""The signing identity `verify-image.sh` accepts, pinned to a table.

`CERT_IDENTITY_RE` in `deploy/scripts/verify-image.sh` is the one place this
project says which builds are *ours*. Everything downstream of it — the
promotion script's refusal to pin an unverifiable digest, CI re-checking the
pinned digest on every push, an operator checking before a restart — is worth
exactly as much as that string, and it was widened when tagged releases
arrived: a certificate's subject ends in the ref that produced it, so a
verifier pinned to `refs/heads/main` rejects every release.

Widening a security boundary deserves a test that says how far, so the
rejections below outnumber the acceptances and include the two shapes that an
unanchored version of this pattern would wave through.

Two limits, stated rather than glossed:

* The pattern is read out of the shell script, not retyped here, so what is
  tested is the string cosign is handed. What is *not* tested is cosign
  itself — no signed image is reachable from a unit test.
* cosign compiles this with Go's ``regexp`` and applies it with
  ``MatchString``; ``re.search`` below is the Python equivalent of that call.
  The one documented divergence is that Python's ``$`` also matches before a
  trailing newline, and no case here contains one.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "verify-image.sh"

BASE = "https://github.com/Darkflib/sre-tab/.github/workflows/ci.yml"


@pytest.fixture(scope="module")
def identity_re() -> re.Pattern[str]:
    """The pattern the script builds, obtained by letting the shell build it."""
    expansion = subprocess.run(
        [
            "sh",
            "-c",
            f"eval \"$(grep -E '^(SOURCE_REPO|CERT_IDENTITY_RE)=' {SCRIPT})\"; "
            'printf "%s" "$CERT_IDENTITY_RE"',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert expansion, "no CERT_IDENTITY_RE assignment found in verify-image.sh"
    return re.compile(expansion)


@pytest.mark.parametrize(
    "san",
    [
        f"{BASE}@refs/heads/main",
        f"{BASE}@refs/tags/v1.1.0",
        f"{BASE}@refs/tags/v1.1.0-rc1",
        f"{BASE}@refs/tags/v10.20.30",
        f"{BASE}@refs/tags/v0.0.1",
    ],
)
def test_the_refs_this_workflow_publishes_from_are_accepted(
    san: str, identity_re: re.Pattern[str]
) -> None:
    assert identity_re.search(san), san


@pytest.mark.parametrize(
    "san",
    [
        # A ref this workflow will not publish from.
        f"{BASE}@refs/heads/main-evil",
        f"{BASE}@refs/heads/mainx",
        f"{BASE}@refs/heads/feature/publish",
        f"{BASE}@refs/pull/12/merge",
        f"{BASE}@refs/tags/nightly",
        # A tag the version resolver would itself have refused.
        f"{BASE}@refs/tags/v1.1",
        f"{BASE}@refs/tags/v01.1.0",
        # Another repository, or another workflow in this one.
        "https://github.com/evil/sre-tab/.github/workflows/ci.yml@refs/heads/main",
        "https://github.com/Darkflib/sre-tab-evil/.github/workflows/ci.yml@refs/heads/main",
        f"{BASE.replace('ci.yml', 'release.yml')}@refs/tags/v1.1.0",
        # The two an unanchored pattern would accept, which is why the
        # pattern is anchored: cosign applies it with an unanchored
        # MatchString and adds no boundaries of its own.
        f"https://evil.example/{BASE}@refs/heads/main",
        f"{BASE}@refs/heads/main#https://evil.example",
        # And the one an unescaped `.` would accept.
        "https://githubXcom/Darkflib/sre-tab/.github/workflows/ci.yml@refs/heads/main",
    ],
)
def test_everything_else_is_rejected(san: str, identity_re: re.Pattern[str]) -> None:
    assert not identity_re.search(san), san
