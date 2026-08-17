"""``python -m app.cli`` — the entry point when the console script is not
on PATH (a source checkout, or a container without the wheel's bin dir)."""

from __future__ import annotations

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
