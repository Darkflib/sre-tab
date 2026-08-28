#!/usr/bin/env python3
"""Check every relative link in the tracked Markdown, target and fragment.

Two failures, and the second is the one this file exists for.

A moved file breaks the *path* half of a link, which the previous version of
this check already caught. A reworded heading breaks the *fragment* half, and
nothing caught that. The second is by far the commoner failure here — files
move rarely, headings get rewritten every time a roadmap entry lands — and it
is also the quieter one, because a link to a missing fragment still resolves:
GitHub serves the page and ignores the anchor, so the reader lands at the top
of a 30KB document and assumes they were sent to the right place.

**Fragments are not slugified.** GitHub derives a heading's anchor with an
algorithm that is neither documented as a contract nor obvious, and the
obvious reimplementation is wrong. Measured against GitHub's own render of
this repository, ``## 2026-08-17 — Phase 0 foundation`` becomes
``#2026-08-17--phase-0-foundation``: the em-dash is stripped and each space
around it becomes its own hyphen, so whitespace is replaced one-for-one
rather than collapsed. Nobody writes that by hand, and a checker that
collapses whitespace would agree with the wrong link and pass it — a false
pass, which is the failure mode this repository has already shipped six
times under other names.

So the fragment is not computed. It is declared, and this only checks that
the declaration exists::

    <a id="branch-protection"></a>
    ## Branch protection

The anchor is a literal string in a file this repository owns, the check is
an exact match, and there is no third-party algorithm anywhere in it. The
shape is fixed — column zero, its own line, immediately above the heading —
so that ``git grep -n '^<a id="' -- '*.md'`` finds every one and a single
``sed`` removes them all if this convention is ever abandoned.

GitHub rewrites the id to ``user-content-<yours>``, the same namespace its
generated heading anchors live in, so a declared anchor and a generated one
resolve identically and adding these breaks no existing link.

Usage::

    python3 .github/scripts/check-doc-links.py

Exits non-zero, listing every problem rather than stopping at the first.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# The fixed shape. Anchored at column zero and matching the whole line, so a
# stray inline `<a id="…">` in prose is not mistaken for a declaration — the
# convention is worth exactly as much as its uniformity.
ANCHOR_RE = re.compile(r'^<a id="([^"]+)"></a>$')

# Markdown inline links. Deliberately not a Markdown parser: the reference
# forms are not used in this repository, and a regex that is obviously
# incomplete is better than a parser that is subtly so.
LINK_RE = re.compile(r"\]\(([^)]+)\)")

SKIP_PREFIXES = ("http://", "https://", "mailto:")

# Inline code spans, removed before links are extracted. A backticked
# `[label](path.md)` is showing the syntax, exactly as a fenced block does,
# and checking it produces a failure the author cannot fix without changing
# what the sentence says.
CODE_SPAN_RE = re.compile(r"`[^`]*`")


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [Path(p) for p in out.split("\0") if p]


def prose_lines(path: Path) -> list[tuple[int, str]]:
    """The document's lines with fenced blocks removed.

    Content inside a fence is an *example* of the convention, not a use of
    it, and this distinction is not hypothetical: the first run of this
    script against CONTRIBUTING.md reported the anchor in its own worked
    example as a duplicate of the real one. The same reasoning applies to
    links — a fenced snippet showing `](path.md)` is illustrating syntax,
    not pointing at a file that has to exist.
    """
    lines: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced:
            lines.append((number, line))
    return lines


def declared_anchors(path: Path) -> tuple[dict[str, int], list[str]]:
    """Return the anchors a document declares, and any duplicates.

    A duplicate is a real defect and not a style nit: two identical ids mean
    a link that looks checked, resolves, and lands somewhere arbitrary.
    """
    anchors: dict[str, int] = {}
    duplicates: list[str] = []
    for number, line in prose_lines(path):
        match = ANCHOR_RE.match(line)
        if not match:
            continue
        anchor = match.group(1)
        if anchor in anchors:
            duplicates.append(
                f"{path}:{number}: duplicate anchor id {anchor!r} (first at line {anchors[anchor]})"
            )
        else:
            anchors[anchor] = number
    return anchors, duplicates


def main() -> int:
    documents = tracked_markdown()
    anchors: dict[Path, dict[str, int]] = {}
    problems: list[str] = []

    for document in documents:
        found, duplicates = declared_anchors(document)
        anchors[document] = found
        problems.extend(duplicates)

    for document in documents:
        targets = [
            target
            for _, line in prose_lines(document)
            for target in LINK_RE.findall(CODE_SPAN_RE.sub("", line))
        ]
        for target in targets:
            if not target or target.startswith(SKIP_PREFIXES):
                continue

            path_part, _, fragment = target.partition("#")

            # An empty path is a link within this same document.
            if path_part:
                # normpath, not resolve: `git ls-files` gives paths relative
                # to the repository root and the anchors dict is keyed on
                # those, so `deploy/../ROADMAP.md` has to collapse to
                # `ROADMAP.md` rather than to an absolute path that never
                # matches a key.
                resolved = Path(os.path.normpath(document.parent / path_part))
                if not resolved.exists():
                    problems.append(f"{document}: broken relative link: {path_part}")
                    continue
            else:
                resolved = document

            if not fragment:
                continue

            known = anchors.get(resolved)
            if known is None:
                # A fragment into something that is not tracked Markdown —
                # an image, say. Nothing to check, and not an error.
                continue
            if fragment not in known:
                problems.append(
                    f"{document}: link to {target!r} but {resolved} declares no "
                    f'<a id="{fragment}"></a>'
                )

    if problems:
        for problem in sorted(problems):
            sys.stderr.write(f"{problem}\n")
        sys.stderr.write(f"\n{len(problems)} problem(s).\n")
        # Only say this when it is the applicable rule. A trailer that
        # explains the anchor convention underneath "this file does not
        # exist" teaches the reader to stop reading trailers.
        if any("declares no" in problem for problem in problems):
            sys.stderr.write(
                "A fragment must name an anchor the target document declares as "
                '`<a id="name"></a>`, at column zero on its own line '
                "— see CONTRIBUTING.md.\n"
            )
        return 1

    checked = sum(len(a) for a in anchors.values())
    sys.stdout.write(
        f"{len(documents)} documents, {checked} declared anchors, all links resolve.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
