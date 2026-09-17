#!/usr/bin/env python3
"""Resolve a git tag into the four things a release build needs to know.

All four are knowable before a single byte reaches the registry, which is
why they are asked here rather than at the step that would use them:

1. **Is this a tag this project publishes?** ``vMAJOR.MINOR.PATCH``, with an
   optional pre-release suffix, and nothing else. A tag that is not that
   shape fails the job rather than pushing something odd — ``v1.1``,
   ``1.1.0``, and ``vfoo`` are all refusals, each with its own reason.
2. **Which registry tags should the image carry?** The exact version always;
   the floating ``MAJOR.MINOR`` only for a final release.
3. **What does CHANGELOG.md say about this version?** The file follows
   `Keep a Changelog <https://keepachangelog.com/en/1.1.0/>`_, so the section
   boundaries are ``## [x.y.z]`` headings and the notes are what sits between
   one and the next. **A missing or empty section is a failure**, not an
   empty release body: a Release with no notes is a green check that verified
   nothing, which is the failure mode this repository has already shipped six
   times under other names.
4. **Do the manifests carry this version?** ``pyproject.toml`` is what the
   application reports as its version, and ``frontend/package.json`` is what
   the page footer shows. A tag that neither was bumped for would publish an
   image named ``1.2.0`` that calls itself ``1.1.0`` everywhere it says
   anything. The comparison is on PEP 440 versions, because Python and npm
   spell a pre-release differently. A tag whose pre-release PEP 440 cannot
   express at all, such as ``-x.7``, is refused for that reason, since no
   build of it could report its own version. ``tests/test_version_parity.py``
   keeps the npm lockfile and the installed distribution in line with these
   two on every push.

Doing all of this before the push matters. A tag whose shape is wrong, or
whose version nobody wrote up, fails a job that has not yet signed, attested,
or published anything — rather than one that has left a publicly pullable
image behind with no Release to explain it.

Usage::

    python3 .github/scripts/release-metadata.py --tag v1.1.0 \\
        [--changelog CHANGELOG.md] [--pyproject pyproject.toml] \\
        [--package-json frontend/package.json] [--notes-out notes.md] \\
        [--print-notes]

Writes a human-readable summary to stdout, the extracted notes to
``--notes-out`` when given, and ``version`` / ``image-tags`` / ``prerelease``
/ ``notes-file`` to ``$GITHUB_OUTPUT`` when that variable is set. Exits
non-zero, with the reason on stderr, for every refusal above.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

# Semantic versioning, anchored, with the leading `v` this project's tags
# carry. Written out rather than borrowed so the refusals can be specific:
# each named group below is something a wrong tag gets told about by name.
#
# Leading zeros are refused because semver refuses them, and because
# `v01.1.0` and `v1.1.0` would otherwise be two tags naming one version.
#
# The pre-release suffix is the same rule applied one level down, and it is
# not the obvious `[0-9A-Za-z-]+`. Semver splits a pre-release into
# dot-separated identifiers and treats them differently: a *numeric*
# identifier — all digits — must not carry a leading zero, because
# pre-releases are ordered and numeric identifiers are compared as numbers,
# so `01` and `1` would be one version wearing two names. An *alphanumeric*
# identifier — one containing at least one letter or hyphen — is compared as
# text and may begin with a zero quite legally. So `-rc01` and `-0alpha` are
# accepted and `-01` and `-alpha.01` are refused, which looks inconsistent
# until you notice which of them are numbers.
SEMVER_RE = re.compile(
    r"""
    ^v
    (?P<major>0|[1-9][0-9]*)\.
    (?P<minor>0|[1-9][0-9]*)\.
    (?P<patch>0|[1-9][0-9]*)
    (?:-(?P<prerelease>(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?
    $
    """,
    re.VERBOSE,
)


def _section_re(version: str) -> re.Pattern[str]:
    """Keep a Changelog's heading for one release.

    The version is matched exactly; whatever follows the bracket — ` -
    2026-08-29`, or nothing at all — is the document's business.
    """
    return re.compile(r"^## \[" + re.escape(version) + r"\]")


# Any level-two heading ends the section, not only the next `## [x.y.z]`.
# The looser boundary is the safe one: a `## Links` or `## Yanked` section
# appearing between releases would otherwise be swallowed into the notes.
HEADING_RE = re.compile(r"^## ")

VERSION_HEADING_RE = re.compile(r"^## \[([^\]]+)\]")


class ReleaseError(Exception):
    """A refusal, with the reason the job's log should carry."""


def parse_tag(tag: str) -> tuple[str, bool]:
    """Return ``(version, is_prerelease)`` for a tag, or raise ``ReleaseError``.

    The version is the tag without its leading ``v``: ``v1.1.0`` -> ``1.1.0``.
    """
    if not tag:
        raise ReleaseError("no tag given")
    if "+" in tag:
        # Refused for a concrete reason rather than on principle: an OCI tag
        # is [A-Za-z0-9_][A-Za-z0-9._-]{0,127}, so semver build metadata
        # cannot be spelled in a registry tag at all. Silently dropping it
        # would publish `1.1.0` for a tag that says something else.
        raise ReleaseError(
            f"refusing {tag!r}: semver build metadata (+) has no legal spelling in an image tag"
        )
    if not tag.startswith("v"):
        raise ReleaseError(f"refusing {tag!r}: a release tag must start with 'v', as in v1.1.0")
    match = SEMVER_RE.match(tag)
    if match is None:
        raise ReleaseError(
            f"refusing {tag!r}: not vMAJOR.MINOR.PATCH — "
            "all three components are required, none may carry a leading zero, "
            "and the only suffix allowed is a semver pre-release such as -rc1"
        )
    version = tag[1:]
    return version, match.group("prerelease") is not None


# PEP 440's pre-release signifiers, and the spellings it normalises to each.
# The standard library has no PEP 440 parser and this script imports nothing
# else, so the subset a release can use is written out here: MAJOR.MINOR.PATCH
# with an optional a, b or rc and an optional number. Epochs, post-releases,
# dev releases and local versions are not things this project tags.
PEP440_SIGNIFIERS = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "rc": "rc",
    "c": "rc",
    "pre": "rc",
    "preview": "rc",
}
_PRE_RELEASE = r"(?P<signifier>alpha|a|beta|b|rc|c|preview|pre)[-_.]?(?P<number>[0-9]+)?"

# A semver pre-release that PEP 440 can also spell: `rc1`, `rc.1`, `rc-1`,
# `rc01`, `alpha`, `beta.2`. Case is not significant to PEP 440.
PEP440_COMPATIBLE_PRE_RELEASE_RE = re.compile(rf"^{_PRE_RELEASE}$", re.IGNORECASE)

PEP440_RELEASE_RE = re.compile(
    rf"^v?(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)(?:[-_.]?{_PRE_RELEASE})?$",
    re.IGNORECASE,
)

#: ``(major, minor, patch, signifier, number)``, with the last two ``None``
#: for a final release: a version as PEP 440 compares it.
ReleaseVersion = tuple[int, int, int, str | None, int | None]


def _pep440(version: ReleaseVersion) -> str:
    major, minor, patch, signifier, number = version
    return f"{major}.{minor}.{patch}" + (f"{signifier}{number}" if signifier else "")


def _from_match(match: re.Match[str], pre_release: re.Match[str] | None) -> ReleaseVersion:
    release = (int(match.group("major")), int(match.group("minor")), int(match.group("patch")))
    if pre_release is None:
        return (*release, None, None)
    signifier = PEP440_SIGNIFIERS[pre_release.group("signifier").lower()]
    return (*release, signifier, int(pre_release.group("number") or 0))


def semver_release_version(version: str, source: str) -> ReleaseVersion:
    """The PEP 440 reading of a semver version, or ``ReleaseError``.

    Used for the tag and for ``package.json``, which npm requires to be
    semver. *source* opens the refusal and says which value it is about.
    """
    match = SEMVER_RE.match(f"v{version}")
    if match is None:
        raise ReleaseError(f"{source}: not MAJOR.MINOR.PATCH semver")
    pre_release = match.group("prerelease")
    if pre_release is None:
        return _from_match(match, None)
    compatible = PEP440_COMPATIBLE_PRE_RELEASE_RE.match(pre_release)
    if compatible is None:
        raise ReleaseError(
            f"{source}: the pre-release {pre_release!r} has no PEP 440 spelling — "
            "PEP 440 knows only a, b and rc (or alpha, beta, c, pre, preview), "
            "each with an optional number — so pyproject.toml could never carry it. "
            "Use a pre-release such as -rc.1."
        )
    return _from_match(match, compatible)


def _pyproject_version(path: Path) -> tuple[ReleaseVersion, str]:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"{path} is not valid TOML: {exc}") from exc
    project = document.get("project")
    raw = project.get("version") if isinstance(project, dict) else None
    if not isinstance(raw, str):
        # A dynamic version would be computed by the build backend, and
        # nothing here should guess what it computes to.
        raise ReleaseError(f"{path} has no static [project] version to compare the tag with")
    match = PEP440_RELEASE_RE.match(raw.strip())
    if match is None:
        raise ReleaseError(
            f"{path} carries {raw!r}, which is not MAJOR.MINOR.PATCH with an optional "
            "a, b or rc pre-release, so no release tag can match it"
        )
    pre_release = match if match.group("signifier") else None
    return _from_match(match, pre_release), raw


def _package_json_version(path: Path) -> tuple[ReleaseVersion, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{path} is not valid JSON: {exc}") from exc
    raw = document.get("version") if isinstance(document, dict) else None
    if not isinstance(raw, str):
        raise ReleaseError(f"{path} has no version to compare the tag with")
    return semver_release_version(raw, f"{path} carries {raw!r}"), raw


def check_manifests(version: str, *, pyproject: Path, package_json: Path) -> None:
    """Refuse a tag the manifests do not carry.

    Both files are read and both mismatches reported, so a release that
    forgot one learns about the other in the same run. A file that is
    missing is a refusal: an absent manifest would otherwise read as an
    agreeing one.
    """
    wanted = semver_release_version(version, f"refusing v{version}")
    mismatches = [
        f"{path} says {raw!r}"
        for path, (found, raw) in (
            (pyproject, _pyproject_version(pyproject)),
            (package_json, _package_json_version(package_json)),
        )
        if found != wanted
    ]
    if mismatches:
        raise ReleaseError(
            f"refusing v{version}: {'; '.join(mismatches)}, not {_pep440(wanted)}. "
            "Bump the version before tagging, or the image would report a version "
            "other than the one it is published as."
        )


def _final_release_versions(tags: Sequence[str]) -> list[tuple[int, int, int]]:
    """Every tag in ``tags`` that names a final release, as a sortable
    triple. Pre-releases and anything that is not a release tag at all are
    dropped rather than raising: the list is whatever `git tag` printed, and
    a repository is entitled to carry tags this project did not mint."""
    out = []
    for tag in tags:
        match = SEMVER_RE.match(tag.strip())
        if match is None or match.group("prerelease") is not None:
            continue
        out.append(
            (int(match.group("major")), int(match.group("minor")), int(match.group("patch")))
        )
    return out


def image_tags(
    version: str, is_prerelease: bool, known_tags: Sequence[str] | None = None
) -> list[str]:
    """The registry tags a build of ``version`` should be published under.

    The exact version always. The floating ``MAJOR.MINOR`` **only for a final
    release**, and this is a deliberate call rather than an oversight:
    ``1.1.0-rc1`` precedes ``1.1.0`` in semver's ordering, so moving ``:1.1``
    onto a release candidate would hand a pre-release to everyone who asked
    for the stable minor line — the one population that explicitly did not
    ask for it. A pre-release is therefore pullable only by its exact name.

    The floating tag is also withheld when ``known_tags`` shows a *higher*
    patch already released on this minor line. A floating tag is the only
    thing here that moves, so it is the only thing that can move backwards,
    and the case is not hypothetical: the concurrency group is keyed on the
    full ref, so two patch releases of one minor line are not serialised
    against each other, and re-running an older tag's job is a button in the
    Actions UI. Either lets ``v1.1.1`` finish after ``v1.1.2`` and quietly
    downgrade everyone following ``:1.1``. The exact version tag is
    unaffected — it names one build and always did.

    There is no floating ``:1``. A tag spanning every minor of a major is a
    larger promise than this project is in a position to keep, and the
    reference deployment pins a digest regardless.
    """
    tags = [version]
    if is_prerelease:
        return tags

    major, minor, patch = (int(part) for part in version.split(".", 2))
    if known_tags is not None:
        superseding = [
            release
            for release in _final_release_versions(known_tags)
            if release[:2] == (major, minor) and release[2] > patch
        ]
        if superseding:
            highest = ".".join(str(part) for part in max(superseding))
            sys.stderr.write(
                f"note: withholding the floating :{major}.{minor} tag — {highest} is already "
                f"released on this line, and moving it to {version} would be a downgrade "
                "for anyone following it\n"
            )
            return tags
    tags.append(f"{major}.{minor}")
    return tags


def changelog_notes(changelog: Path, version: str) -> str:
    """The body of ``## [version]`` in a Keep a Changelog file.

    Raises ``ReleaseError`` if the section is absent or empty. Both are the
    same failure from the reader's point of view — a Release that explains
    nothing — so both stop the build.
    """
    try:
        lines = changelog.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseError(f"cannot read {changelog}: {exc}") from exc

    wanted = _section_re(version)
    start: int | None = None
    for index, line in enumerate(lines):
        if wanted.match(line):
            start = index + 1
            break

    if start is None:
        known = [m.group(1) for line in lines if (m := VERSION_HEADING_RE.match(line))]
        raise ReleaseError(
            f"{changelog} has no '## [{version}]' section. "
            f"Sections present: {', '.join(known) if known else 'none'}. "
            "A release with empty notes is a green check that verified nothing; "
            "write the section before tagging."
        )

    end = len(lines)
    for index in range(start, len(lines)):
        if HEADING_RE.match(lines[index]):
            end = index
            break

    body = "\n".join(lines[start:end]).strip("\n")
    if not body.strip():
        raise ReleaseError(
            f"{changelog}'s '## [{version}]' section is empty. "
            "A release with empty notes is a green check that verified nothing."
        )
    return body + "\n"


def _read_git_tags(path: Path | None, current: str) -> list[str] | None:
    """The tag list to judge the floating tag against, or ``None`` for "no
    list was offered, so do not judge".

    The one assertion here earns its place. A caller that passes a tag file
    is relying on it to withhold a backwards-moving floating tag, and the
    likeliest way for that to fail is not a wrong answer but an empty file —
    a checkout that fetched no tags answers "nothing is newer" for every
    version, which is indistinguishable from a correct pass and silently
    restores the behaviour the file was added to prevent. The tag being
    built is necessarily in its own repository's tag list, so its absence
    means the list is not one, and that is worth failing on.
    """
    if path is None:
        return None
    try:
        tags = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc
    tags = [tag for tag in tags if tag]
    if current not in tags:
        raise ReleaseError(
            f"{path} does not list {current!r}, the tag being built, so it is not a complete "
            "tag list — most likely a checkout without fetch-tags. Refusing to decide the "
            "floating tag from it, because an empty list silently answers 'nothing is newer'."
        )
    return tags


def _write_github_output(pairs: dict[str, str]) -> None:
    """Append step outputs, if this is running inside a job that wants them."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in pairs.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a release tag into image tags and CHANGELOG notes."
    )
    parser.add_argument("--tag", required=True, help="the git tag, e.g. v1.1.0")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="the Keep a Changelog file to read (default: CHANGELOG.md)",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="the manifest whose version the application reports (default: pyproject.toml)",
    )
    parser.add_argument(
        "--package-json",
        type=Path,
        default=Path("frontend/package.json"),
        help=("the manifest whose version the page footer shows (default: frontend/package.json)"),
    )
    parser.add_argument(
        "--notes-out",
        type=Path,
        default=None,
        help="write the extracted notes here",
    )
    parser.add_argument(
        "--print-notes",
        action="store_true",
        help="also write the extracted notes to stdout",
    )
    parser.add_argument(
        "--git-tags-file",
        type=Path,
        default=None,
        help=(
            "a file of newline-separated git tags, as `git tag --list` prints them. "
            "Used only to decide whether the floating MAJOR.MINOR tag would move "
            "backwards; omitted, the floating tag is always offered."
        ),
    )
    args = parser.parse_args(argv)

    try:
        version, is_prerelease = parse_tag(args.tag)
        check_manifests(version, pyproject=args.pyproject, package_json=args.package_json)
        known = _read_git_tags(args.git_tags_file, args.tag)
        tags = image_tags(version, is_prerelease, known)
        notes = changelog_notes(args.changelog, version)
    except ReleaseError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if args.notes_out is not None:
        args.notes_out.write_text(notes, encoding="utf-8")

    _write_github_output(
        {
            "version": version,
            "image-tags": " ".join(tags),
            "prerelease": "true" if is_prerelease else "false",
            "notes-file": str(args.notes_out) if args.notes_out else "",
        }
    )

    sys.stdout.write(
        f"tag          {args.tag}\n"
        f"version      {version}\n"
        f"pre-release  {'yes' if is_prerelease else 'no'}\n"
        f"manifests    {args.pyproject} and {args.package_json} agree\n"
        f"image tags   {' '.join(tags)}\n"
        f"notes        {len(notes.splitlines())} line(s) from "
        f"{args.changelog} [{version}]\n"
    )
    if args.print_notes:
        sys.stdout.write("---\n" + notes + "---\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
