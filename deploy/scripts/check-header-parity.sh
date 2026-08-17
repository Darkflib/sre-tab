#!/bin/sh
#
# Fail if the security headers Caddy sets on statically served files have
# drifted from the ones app/middleware.py sets on API responses.
#
# The two exist for different paths — the middleware never sees a file Caddy
# serves from disk, and the SPA document is where CSP is actually enforced —
# so the mirror is necessary. This check is what stops it becoming a lie: edit
# _CSP or _STATIC_HEADERS without editing the Caddyfile and CI says so.
#
#   PYTHON_CMD="uv run python" deploy/scripts/check-header-parity.sh

set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
: "${PYTHON_CMD:=uv run python}"

cd "$repo_root"

# shellcheck disable=SC2086  # PYTHON_CMD is a command line, split deliberately
exec $PYTHON_CMD - "$repo_root/deploy/Caddyfile" <<'PY'
import re
import sys

from app.middleware import _CSP, _STATIC_HEADERS

BEGIN = "--- BEGIN mirrored from app/middleware.py ---"
END = "--- END mirrored from app/middleware.py ---"

caddyfile = sys.argv[1]
with open(caddyfile, encoding="utf-8") as handle:
    text = handle.read()

try:
    block = text.split(BEGIN, 1)[1].split(END, 1)[0]
except IndexError:
    sys.exit(f"{caddyfile}: mirror markers not found")

found = dict(re.findall(r'^\s*([A-Za-z][A-Za-z-]*)\s+"(.*)"\s*$', block, re.MULTILINE))
expected = {"Content-Security-Policy": _CSP, **_STATIC_HEADERS}

problems = []
for name, value in sorted(expected.items()):
    if name not in found:
        problems.append(f"missing from the Caddyfile: {name}")
    elif found[name] != value:
        problems.append(
            f"{name} differs\n"
            f"  app/middleware.py: {value}\n"
            f"  deploy/Caddyfile:  {found[name]}"
        )
for name in sorted(set(found) - set(expected)):
    problems.append(f"in the Caddyfile but not in app/middleware.py: {name}")

if problems:
    sys.exit(
        "security header drift between app/middleware.py and deploy/Caddyfile:\n\n"
        + "\n".join(problems)
        + "\n\nUpdate the block between the mirror markers in deploy/Caddyfile."
    )

print(f"security headers match ({len(expected)} headers)")
PY
