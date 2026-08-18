#!/usr/bin/env python3
"""Execute the fenced code blocks a Markdown document marks as runnable.

A README that nobody executes rots quietly, and this project has already
paid for that twice: ``install.sh --start`` never recreated a removed
network, and the documented upgrade procedure was wrong as written. Both
read perfectly.

The fix is not to copy the commands into a workflow — a copy drifts, which
is the same failure one level removed. Instead the document itself is the
script. A block is opted in with an HTML comment on the line before it,
invisible in every Markdown renderer::

    <!-- docs:run -->
    ```sh
    uv sync
    ```

    <!-- docs:run background ready=http://localhost:8000/api/v1/healthz -->
    ```sh
    uv run uvicorn app.main:app --reload
    ```

Ordinary blocks run to completion and must exit zero. A ``background``
block is started as a job and the runner then waits for ``ready=`` to
answer, which is the harness equivalent of "open a second terminal";
everything still running is killed when the script ends.

Blocks are concatenated into one bash script in document order, so a
reader following the document top to bottom sees exactly what ran. Each
block starts from the repository root, so a ``cd`` inside one does not
leak into the next.

Usage::

    python3 .github/scripts/run-doc-examples.py README.md
    python3 .github/scripts/run-doc-examples.py README.md --print

It runs the commands **for real** in ``--root`` (default: the directory
holding the document), including anything that overwrites a file. Point it
at a throwaway clone, not at a working tree you care about.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MARKER = re.compile(r"^\s*<!--\s*docs:run(?P<attrs>[^>]*?)-->\s*$")
FENCE = re.compile(r"^\s*(?P<fence>```+|~~~+)(?P<info>.*)$")

#: How long a backgrounded block gets to answer its readiness URL. Long
#: enough for a cold `npm ci` plus a Vite start, short enough that a job
#: which will never come up does not burn a CI minute.
READY_TIMEOUT_SECONDS = 120


class DocError(Exception):
    """A problem with the document, not with the commands it contains."""


@dataclass(frozen=True)
class Block:
    line: int
    body: str
    background: bool
    ready_url: str | None


def _parse_attrs(raw: str, line: int) -> tuple[bool, str | None]:
    background = False
    ready_url: str | None = None
    for token in raw.split():
        if token == "background":
            background = True
        elif token.startswith("ready="):
            ready_url = token.removeprefix("ready=")
        else:
            raise DocError(f"line {line}: unknown docs:run attribute {token!r}")
    if background and ready_url is None:
        raise DocError(f"line {line}: a background block needs ready=<url>")
    if ready_url is not None and not background:
        raise DocError(f"line {line}: ready= only means anything with background")
    return background, ready_url


def _read_fence(lines: list[str], index: int, fence: str) -> tuple[list[str], int]:
    """Return a fenced block's body and the index just past its closing fence.

    ``index`` points at the line after the opening fence. A closing fence has
    to be at least as long as the opening one, so a block opened with four
    backticks may quote three of them — which is how CONTRIBUTING.md shows
    this file's own marker syntax without the sample becoming live.
    """
    body: list[str] = []
    while index < len(lines):
        closing = FENCE.match(lines[index])
        if closing is not None and closing.group("fence").startswith(fence):
            return body, index + 1
        body.append(lines[index])
        index += 1
    return body, -1


def parse(document: str) -> list[Block]:
    """Collect every fenced block preceded by a ``docs:run`` marker."""
    lines = document.splitlines()
    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        unmarked = FENCE.match(lines[index])
        if unmarked is not None:
            # A block nobody opted in: skip it whole rather than scanning
            # inside it, so a marker quoted in a code sample stays a sample.
            _, index = _read_fence(lines, index + 1, unmarked.group("fence"))
            if index < 0:
                break
            continue

        marker = MARKER.match(lines[index])
        if marker is None:
            index += 1
            continue

        marker_line = index + 1
        background, ready_url = _parse_attrs(marker.group("attrs"), marker_line)

        # Allow a blank line between the marker and its block; anything
        # else means the marker has drifted away from what it annotates.
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        opening = FENCE.match(lines[index]) if index < len(lines) else None
        if opening is None:
            raise DocError(f"line {marker_line}: docs:run is not followed by a fenced block")

        body, index = _read_fence(lines, index + 1, opening.group("fence"))
        if index < 0:
            raise DocError(f"line {marker_line}: fenced block is never closed")

        blocks.append(
            Block(
                line=marker_line,
                body="\n".join(body),
                background=background,
                ready_url=ready_url,
            )
        )
    return blocks


def render(blocks: list[Block], *, doc: str, root: Path) -> str:
    """Turn the blocks into one bash script."""
    parts = [
        "#!/usr/bin/env bash",
        "# Generated by .github/scripts/run-doc-examples.py — do not edit.",
        "set -euo pipefail",
        # Job control puts each background block in its own process group,
        # so the whole tree started by `uv run uvicorn` or `npm run dev`
        # can be signalled, not just the shell that launched it.
        "set -m",
        "",
        f"docs_root={shlex.quote(str(root))}",
        "docs_pids=()",
        "",
        # Signal the process *group*, not the pid: `uv run uvicorn` and
        # `npm run dev` each leave a tree behind, and killing only the shell
        # that started them orphans the server still holding the port. TERM
        # first so a graceful shutdown happens, KILL after a grace period so
        # one that ignores TERM does not outlive the run either.
        #
        # This is a trap, so it does nothing if the harness is killed
        # outright (a CI job cancellation, a SIGKILL). CI discards the runner
        # so it never matters there; locally the next run says "an address
        # already in use is the usual cause" and stops in seconds.
        "docs_cleanup() {",
        "  local pid",
        '  for pid in "${docs_pids[@]:-}"; do',
        '    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true',
        "  done",
        "  [ ${#docs_pids[@]} -eq 0 ] || sleep 2",
        '  for pid in "${docs_pids[@]:-}"; do',
        '    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true',
        "  done",
        "}",
        "trap docs_cleanup EXIT",
        "",
        "docs_wait_ready() {",
        "  local pid=$1 url=$2 attempt",
        f"  for attempt in $(seq 1 {READY_TIMEOUT_SECONDS}); do",
        # --max-time matters more than it looks. Without it, a process that
        # accepts the connection and then answers nothing -- something else
        # already on the port, a half-started server -- hangs curl, and the
        # loop's own timeout never gets a turn. That is the difference
        # between a two-minute failure and a workflow someone disables.
        '    if curl --fail --silent --max-time 5 --output /dev/null "$url"; then',
        '      echo "docs: $url answered after ${attempt}s"',
        "      return 0",
        "    fi",
        # A background block that has already exited is never going to
        # answer. Say so straight away and name the likely cause, rather
        # than spending the full budget discovering it.
        '    if ! kill -0 "$pid" 2>/dev/null; then',
        '      echo "docs: the block exited before $url answered; its output is above." >&2',
        '      echo "docs: an address already in use is the usual cause." >&2',
        "      return 1",
        "    fi",
        "    sleep 1",
        "  done",
        f'  echo "docs: $url did not answer within {READY_TIMEOUT_SECONDS}s" >&2',
        "  return 1",
        "}",
        "",
    ]
    for block in blocks:
        kind = "background" if block.background else "run"
        parts.append(f"echo '::group::{doc}:{block.line} ({kind})'")
        parts.append("(")
        parts.append('  cd "$docs_root"')
        parts.append("  set -x")
        parts.extend(f"  {line}" if line.strip() else line for line in block.body.splitlines())
        if block.background:
            # A scalar rather than ${docs_pids[-1]}: negative subscripts
            # arrived in bash 4.3, and macOS still ships 3.2 as /bin/bash.
            parts.append(") &")
            parts.append("docs_pid=$!")
            parts.append('docs_pids+=("$docs_pid")')
            parts.append(f'docs_wait_ready "$docs_pid" {shlex.quote(block.ready_url or "")}')
        else:
            parts.append(")")
        parts.append("echo '::endgroup::'")
        parts.append("")
    return "\n".join(parts)


def _say(message: str) -> None:
    # Not `print`: the project's Ruff configuration reserves that for the
    # operator CLI (T20), and this is a build tool, not a user interface.
    sys.stderr.write(f"{message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("document", type=Path, help="Markdown file to execute.")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Directory each block runs from (default: the document's directory).",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Write the generated script to stdout and exit without running it.",
    )
    args = parser.parse_args(argv)

    document: Path = args.document
    root: Path = (args.root or document.parent).resolve()

    try:
        blocks = parse(document.read_text(encoding="utf-8"))
    except DocError as exc:
        _say(f"error: {document}: {exc}")
        return 2

    if not blocks:
        # The whole point is that the document stays executable. A
        # rewrite that drops the markers must fail loudly rather than
        # leaving a workflow that checks nothing and stays green.
        _say(f"error: {document} contains no docs:run blocks")
        return 2

    # The path rather than the basename: two different README.md files are
    # executed now, and `README.md:58` in a CI log does not say which.
    script = render(blocks, doc=str(document), root=root)
    if args.print_only:
        sys.stdout.write(f"{script}\n")
        return 0

    _say(f"{document}: running {len(blocks)} docs:run block(s) in {root}")
    with tempfile.NamedTemporaryFile("w", suffix=".sh", encoding="utf-8") as handle:
        handle.write(script)
        handle.flush()
        return subprocess.run(["bash", handle.name], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
