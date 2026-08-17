#!/bin/sh
#
# Fail if .env.example has drifted from app/settings.py.
#
# AGENTS.md makes keeping the two in step agent E's job. This turns that
# promise into a gate: settings.py is Phase 0 property and will grow in later
# phases, and an undocumented environment variable is one an operator cannot
# set — which lands as a production surprise rather than a review comment.
#
#   PYTHON_CMD="uv run python" deploy/scripts/check-env-example.sh

set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
: "${PYTHON_CMD:=uv run python}"

cd "$repo_root"

# shellcheck disable=SC2086  # PYTHON_CMD is a command line, split deliberately
exec $PYTHON_CMD - <<'PY'
import re
import sys

from app.settings import Settings

with open(".env.example", encoding="utf-8") as handle:
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", handle.read(), re.MULTILINE))

expected = {name.upper() for name in Settings.model_fields}

missing = sorted(expected - documented)
extra = sorted(documented - expected)

problems = []
if missing:
    problems.append(
        "settings with no entry in .env.example:\n  " + "\n  ".join(missing)
    )
if extra:
    problems.append(
        "entries in .env.example that are not settings:\n  " + "\n  ".join(extra)
    )

if problems:
    sys.exit(
        "drift between app/settings.py and .env.example:\n\n" + "\n\n".join(problems)
    )

print(f".env.example documents all {len(expected)} settings")
PY
