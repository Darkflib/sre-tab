"""The Settings page offers exactly the languages the server accepts.

``frontend/src/lib/languages.ts`` carries a copy of
:data:`app.ingest.language.LANGUAGE_CODES`, because the browser needs the
list and the contract has no enum to generate it from. A copy drifts, and
either direction is a bug a reader meets: a code only in the frontend is a
choice the save refuses, and one only on the server is a language nobody
can pick. So the copy is read here and held to the original.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.ingest.language import LANGUAGE_CODES

SOURCE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "languages.ts"
BLOCK = re.compile(r"// BEGIN LANGUAGE_CODES\n(.*?)// END LANGUAGE_CODES", re.DOTALL)


def test_the_frontend_offers_exactly_the_detector_codes() -> None:
    match = BLOCK.search(SOURCE.read_text(encoding="utf-8"))
    assert match is not None, f"no LANGUAGE_CODES block in {SOURCE}"
    codes = re.findall(r"'([a-z]+)'", match.group(1))

    # Not only equal as sets: a repeated code would be a duplicate option.
    assert len(codes) == len(set(codes))
    assert set(codes) == LANGUAGE_CODES
