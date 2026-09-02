"""The release resolver has to refuse, not merely exist.

`.github/scripts/release-metadata.py` is what stands between a mistyped tag
and a signed, attested, publicly pullable image published under a name nobody
meant — and between a tag nobody wrote up and a GitHub Release with an empty
body. Both are the same shape of failure this repository has shipped six
times: a step that ran, reported success, and asserted nothing.

So the refusals are exercised first and at length, through the command-line
entry point rather than the functions, because the entry point is what the
workflow actually invokes and the exit status is what the job actually reads.
The acceptances come last, deliberately: a resolver that refuses everything
is as useless as one that accepts everything.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "release-metadata.py"

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Something not yet released.

## [1.1.0] - 2026-09-02

### Added

- A tag-triggered publish path.

### Changed

- `:latest` stays on main.

## [1.0.0] - 2026-08-29

### Added

- The first release.
"""

# A section that exists and says nothing. Distinct from an absent section in
# the document and identical to it for the reader, which is why both fail.
EMPTY_SECTION = """# Changelog

## [2.0.0] - 2026-09-02

## [1.0.0] - 2026-08-29

- The first release.
"""


@pytest.fixture
def changelog(tmp_path: Path) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG)
    return path


def resolve(
    tag: str, changelog: Path, *, notes_out: Path | None = None
) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT), "--tag", tag, "--changelog", str(changelog)]
    if notes_out is not None:
        argv += ["--notes-out", str(notes_out)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


# --- Refusals -----------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v1.1", "not vMAJOR.MINOR.PATCH"),
        ("v1", "not vMAJOR.MINOR.PATCH"),
        ("1.1.0", "must start with 'v'"),
        ("vfoo", "not vMAJOR.MINOR.PATCH"),
        ("v1.1.0.1", "not vMAJOR.MINOR.PATCH"),
        ("v01.1.0", "not vMAJOR.MINOR.PATCH"),
        ("v1.1.0+build.5", "build metadata"),
        ("release-1.1.0", "must start with 'v'"),
        ("v1.1.0 ", "not vMAJOR.MINOR.PATCH"),
    ],
)
def test_a_tag_that_is_not_a_version_is_refused(tag: str, expected: str, changelog: Path) -> None:
    result = resolve(tag, changelog)
    assert result.returncode != 0, f"{tag!r} was accepted"
    assert expected in result.stderr
    # Nothing partial escapes on the way out: the workflow reads stdout only
    # on success, and a half-resolved tag on stdout would be worse than none.
    assert result.stdout == ""


def test_a_version_with_no_changelog_section_is_refused(changelog: Path) -> None:
    result = resolve("v9.9.9", changelog)
    assert result.returncode != 0
    assert "no '## [9.9.9]' section" in result.stderr
    # The refusal names what the file does have, so the fix is obvious from
    # the job log without opening the document.
    assert "Unreleased, 1.1.0, 1.0.0" in result.stderr


def test_a_changelog_section_that_exists_but_is_empty_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(EMPTY_SECTION)
    result = resolve("v2.0.0", path)
    assert result.returncode != 0
    assert "is empty" in result.stderr


def test_a_missing_changelog_is_refused(tmp_path: Path) -> None:
    result = resolve("v1.1.0", tmp_path / "absent.md")
    assert result.returncode != 0
    assert "cannot read" in result.stderr


def test_a_refused_tag_writes_no_notes_file(changelog: Path, tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    assert resolve("v1.1", changelog, notes_out=notes).returncode != 0
    assert not notes.exists()


# --- Acceptances --------------------------------------------------------


def test_a_final_release_gets_the_floating_minor_tag(changelog: Path) -> None:
    result = resolve("v1.1.0", changelog)
    assert result.returncode == 0, result.stderr
    assert "version      1.1.0" in result.stdout
    assert "image tags   1.1.0 1.1" in result.stdout
    assert "pre-release  no" in result.stdout


def test_a_pre_release_does_not_move_the_floating_minor_tag(changelog: Path) -> None:
    """The decision this project made, pinned so it cannot drift back.

    `1.1.0-rc1` sorts *below* `1.1.0`, so a `:1.1` pointing at a release
    candidate would hand a pre-release to the people who asked for the
    stable line. It is publishable only under its exact name.
    """
    tmp = changelog.parent / "rc.md"
    tmp.write_text(CHANGELOG.replace("## [1.1.0] - 2026-09-02", "## [1.1.0-rc1] - 2026-09-01"))
    result = resolve("v1.1.0-rc1", tmp)
    assert result.returncode == 0, result.stderr
    assert "image tags   1.1.0-rc1\n" in result.stdout
    assert "1.1\n" not in result.stdout.replace("1.1.0-rc1", "")
    assert "pre-release  yes" in result.stdout


def test_the_notes_are_the_section_and_stop_at_the_next_heading(
    changelog: Path, tmp_path: Path
) -> None:
    notes = tmp_path / "notes.md"
    assert resolve("v1.1.0", changelog, notes_out=notes).returncode == 0
    body = notes.read_text()
    assert "A tag-triggered publish path." in body
    assert "`:latest` stays on main." in body
    # The boundaries hold in both directions: neither the release above nor
    # the one below leaks in.
    assert "Something not yet released." not in body
    assert "The first release." not in body
    assert not body.startswith("\n")


def test_step_outputs_are_written_for_the_workflow(
    changelog: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow reads these four keys; a rename here breaks it silently."""
    output = tmp_path / "github_output"
    output.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    notes = tmp_path / "notes.md"
    argv = [
        sys.executable,
        str(SCRIPT),
        "--tag",
        "v1.1.0",
        "--changelog",
        str(changelog),
        "--notes-out",
        str(notes),
    ]
    assert subprocess.run(argv, capture_output=True, text=True, check=False).returncode == 0
    written = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    assert written == {
        "version": "1.1.0",
        "image-tags": "1.1.0 1.1",
        "prerelease": "false",
        "notes-file": str(notes),
    }
