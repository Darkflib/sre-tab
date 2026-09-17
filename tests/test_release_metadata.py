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

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "release-metadata.py"

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


def manifests(directory: Path, *, python: str, npm: str) -> tuple[Path, Path]:
    """A ``pyproject.toml`` and a ``package.json`` carrying the given versions."""
    directory.mkdir(parents=True, exist_ok=True)
    pyproject = directory / "pyproject.toml"
    pyproject.write_text(f'[project]\nname = "sre-tab"\nversion = "{python}"\n')
    package_json = directory / "package.json"
    package_json.write_text(json.dumps({"name": "frontend", "version": npm}) + "\n")
    return pyproject, package_json


def resolve(
    tag: str,
    changelog: Path,
    *,
    notes_out: Path | None = None,
    git_tags: Path | None = None,
    pyproject: Path | None = None,
    package_json: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the resolver as the workflow does.

    Manifests that agree with the tag are written unless a test passes its
    own, so the tests about shape, tags, and notes stay about those. The
    tag's version goes into both files verbatim, which PEP 440 accepts for
    every pre-release it can spell at all.
    """
    if pyproject is None or package_json is None:
        default_pyproject, default_package_json = manifests(
            changelog.parent / "agreeing-manifests", python=tag[1:], npm=tag[1:]
        )
        pyproject = pyproject or default_pyproject
        package_json = package_json or default_package_json
    argv = [
        sys.executable,
        str(SCRIPT),
        "--tag",
        tag,
        "--changelog",
        str(changelog),
        "--pyproject",
        str(pyproject),
        "--package-json",
        str(package_json),
    ]
    if notes_out is not None:
        argv += ["--notes-out", str(notes_out)]
    if git_tags is not None:
        argv += ["--git-tags-file", str(git_tags)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def tag_list(tmp_path: Path, *tags: str) -> Path:
    path = tmp_path / "git-tags.txt"
    path.write_text("\n".join(tags) + ("\n" if tags else ""))
    return path


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
        # Semver orders numeric pre-release identifiers as numbers, so a
        # leading zero makes one version answer to two names. An identifier
        # carrying a letter is text and is not subject to the rule, which is
        # why -rc01 is accepted below and these are not.
        ("v1.1.0-01", "not vMAJOR.MINOR.PATCH"),
        ("v1.1.0-alpha.01", "not vMAJOR.MINOR.PATCH"),
        ("v1.1.0-1.007", "not vMAJOR.MINOR.PATCH"),
        ("v1.1.0-", "not vMAJOR.MINOR.PATCH"),
        ("v1.1.0-alpha..1", "not vMAJOR.MINOR.PATCH"),
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
    pyproject, package_json = manifests(tmp_path / "manifests", python="1.1.0", npm="1.1.0")
    argv = [
        sys.executable,
        str(SCRIPT),
        "--tag",
        "v1.1.0",
        "--changelog",
        str(changelog),
        "--pyproject",
        str(pyproject),
        "--package-json",
        str(package_json),
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


# --- Pre-release identifiers that are legal and look as though they are not --


def _changelog_for(tmp_path: Path, version: str) -> Path:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"# Changelog\n\n## [{version}]\n\n- Something.\n")
    return changelog


@pytest.mark.parametrize(
    "tag",
    [
        "v1.1.0-rc01",  # alphanumeric: compared as text, may lead with zero
        "v1.1.0-alpha.1",
    ],
)
def test_a_legal_pre_release_identifier_is_accepted(tag: str, tmp_path: Path) -> None:
    """The mirror of the refusals above, and the reason they are narrow.

    Semver's rule is about *numeric* identifiers only, so tightening the
    pattern far enough to catch `-01` is very easy to overshoot into
    rejecting `-rc01`, which is legal and is the spelling a person is most
    likely to reach for.
    """
    version = tag[1:]

    result = resolve(tag, _changelog_for(tmp_path, version))

    assert result.returncode == 0, result.stderr
    assert version in result.stdout


@pytest.mark.parametrize(
    "tag",
    [
        "v1.1.0-0alpha",
        "v1.1.0-0",  # the one numeric identifier a zero may spell
        "v1.1.0-x-y-z.1",
    ],
)
def test_a_legal_pre_release_pyproject_cannot_spell_is_refused_for_that_reason(
    tag: str, tmp_path: Path
) -> None:
    """Legal semver, and passed by the shape check, which is the half these
    tags still prove: the refusal names PEP 440, not the tag's shape. They
    are refused because ``pyproject.toml`` could never carry the version, so
    no build of the tag could report the version it is published as."""
    result = resolve(tag, _changelog_for(tmp_path, tag[1:]))

    assert result.returncode != 0
    assert "has no PEP 440 spelling" in result.stderr
    assert "not vMAJOR.MINOR.PATCH" not in result.stderr


# --- The floating tag never moves backwards -----------------------------


def test_the_floating_tag_is_withheld_when_a_higher_patch_is_released(
    changelog: Path, tmp_path: Path
) -> None:
    """A re-run of v1.1.0's job after v1.1.1 shipped must not drag :1.1 back.

    The concurrency group is keyed on the full ref, so two patch builds of
    one minor line are not serialised against each other, and re-running a
    completed job is a button in the Actions UI. Neither is exotic.
    """
    tags = tag_list(tmp_path, "v1.0.0", "v1.1.0", "v1.1.1")

    result = resolve("v1.1.0", changelog, git_tags=tags)

    assert result.returncode == 0, result.stderr
    assert "1.1.0" in result.stdout
    assert "withholding the floating :1.1 tag" in result.stderr
    # The exact version tag is unaffected: it names one build and always did.
    assert " 1.1\n" not in result.stdout and result.stdout.rstrip().split()[-1] != "1.1"


def test_the_floating_tag_moves_for_the_highest_patch_on_the_line(
    tmp_path: Path,
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [1.1.2]\n\n- Newest.\n")
    tags = tag_list(tmp_path, "v1.1.0", "v1.1.1", "v1.1.2")

    result = resolve("v1.1.2", changelog, git_tags=tags)

    assert result.returncode == 0, result.stderr
    assert "withholding" not in result.stderr


def test_a_higher_patch_on_another_minor_line_does_not_withhold(
    changelog: Path, tmp_path: Path
) -> None:
    """`:1.1` and `:1.2` are different tags. v1.2.9 existing says nothing
    about whether v1.1.0 should own the 1.1 line."""
    tags = tag_list(tmp_path, "v1.1.0", "v1.2.9")

    result = resolve("v1.1.0", changelog, git_tags=tags)

    assert result.returncode == 0, result.stderr
    assert "withholding" not in result.stderr


def test_a_higher_pre_release_does_not_withhold_the_floating_tag(
    changelog: Path, tmp_path: Path
) -> None:
    """v1.1.1-rc1 is not a release. It never moved :1.1 and cannot block it."""
    tags = tag_list(tmp_path, "v1.1.0", "v1.1.1-rc1")

    result = resolve("v1.1.0", changelog, git_tags=tags)

    assert result.returncode == 0, result.stderr
    assert "withholding" not in result.stderr


def test_a_tag_list_missing_the_tag_being_built_is_refused(changelog: Path, tmp_path: Path) -> None:
    """The failure mode this guard exists for is not a wrong answer, it is an
    empty file: a checkout that fetched no tags answers "nothing is newer"
    for every version, which is indistinguishable from a correct pass."""
    tags = tag_list(tmp_path, "v1.0.0")

    result = resolve("v1.1.0", changelog, git_tags=tags)

    assert result.returncode != 0
    assert "not a complete tag list" in result.stderr


def test_an_empty_tag_list_is_refused(changelog: Path, tmp_path: Path) -> None:
    result = resolve("v1.1.0", changelog, git_tags=tag_list(tmp_path))

    assert result.returncode != 0
    assert "not a complete tag list" in result.stderr


def test_tags_this_project_did_not_mint_are_ignored_not_fatal(
    changelog: Path, tmp_path: Path
) -> None:
    """A repository may carry tags of any shape. Only release tags are read."""
    tags = tag_list(tmp_path, "v1.1.0", "nightly", "v-broken", "2026-01-01", "")

    result = resolve("v1.1.0", changelog, git_tags=tags)

    assert result.returncode == 0, result.stderr


# --- The manifests carry the tag's version -------------------------------


def test_a_tag_neither_manifest_was_bumped_for_is_refused_before_anything_is_written(
    changelog: Path, tmp_path: Path
) -> None:
    """The failure this check exists for: a tag pushed without a version
    bump, which would publish `:1.2.0` for an image that reports 1.1.0."""
    changelog.write_text(CHANGELOG + "\n## [1.2.0]\n\n- Next.\n")
    pyproject, package_json = manifests(tmp_path / "m", python="1.1.0", npm="1.1.0")
    notes = tmp_path / "notes.md"

    result = resolve(
        "v1.2.0", changelog, notes_out=notes, pyproject=pyproject, package_json=package_json
    )

    assert result.returncode != 0
    assert str(pyproject) in result.stderr
    assert str(package_json) in result.stderr
    assert "not 1.2.0" in result.stderr
    assert not notes.exists()


@pytest.mark.parametrize(
    ("python", "npm", "stale"),
    [
        ("1.1.0", "1.2.0", "pyproject"),
        ("1.2.0", "1.1.0", "package_json"),
    ],
)
def test_one_forgotten_manifest_is_named_and_the_other_is_not(
    python: str, npm: str, stale: str, tmp_path: Path
) -> None:
    pyproject, package_json = manifests(tmp_path / "m", python=python, npm=npm)

    result = resolve(
        "v1.2.0",
        _changelog_for(tmp_path, "1.2.0"),
        pyproject=pyproject,
        package_json=package_json,
    )

    assert result.returncode != 0
    named, unnamed = (
        (pyproject, package_json) if stale == "pyproject" else (package_json, pyproject)
    )
    assert str(named) in result.stderr
    assert str(unnamed) not in result.stderr


@pytest.mark.parametrize(
    ("tag", "python", "npm"),
    [
        ("v1.2.0-rc.1", "1.2.0rc1", "1.2.0-rc.1"),
        ("v1.2.0-rc1", "1.2.0-rc.1", "1.2.0-rc01"),
        ("v1.2.0-rc.1", "1.2.0.RC1", "1.2.0-c1"),
        ("v1.2.0-rc.1", "1.2.0-pre1", "1.2.0-preview.1"),
        ("v1.2.0-alpha.3", "1.2.0a3", "1.2.0-a.3"),
        ("v1.2.0-beta", "1.2.0b0", "1.2.0-beta.0"),
    ],
)
def test_one_version_spelt_differently_by_python_and_npm_is_accepted(
    tag: str, python: str, npm: str, tmp_path: Path
) -> None:
    """PEP 440 and semver write a pre-release differently, and PEP 440
    normalises several spellings of each signifier. Compared as strings,
    every one of these would be a false refusal of a correct release."""
    pyproject, package_json = manifests(tmp_path / "m", python=python, npm=npm)

    result = resolve(
        tag, _changelog_for(tmp_path, tag[1:]), pyproject=pyproject, package_json=package_json
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("tag", "python"),
    [
        ("v1.2.0-rc.2", "1.2.0rc1"),  # the number is compared
        ("v1.2.0-beta.1", "1.2.0rc1"),  # and the signifier
        ("v1.2.0", "1.2.0rc1"),  # a final release is not its own release candidate
        ("v1.2.0-rc.1", "1.2.0"),
        ("v1.2.1", "1.2.0"),
        ("v2.2.0", "1.2.0"),
    ],
)
def test_versions_that_differ_in_any_part_are_refused(
    tag: str, python: str, tmp_path: Path
) -> None:
    pyproject, package_json = manifests(tmp_path / "m", python=python, npm=tag[1:])

    result = resolve(
        tag, _changelog_for(tmp_path, tag[1:]), pyproject=pyproject, package_json=package_json
    )

    assert result.returncode != 0
    assert str(pyproject) in result.stderr


@pytest.mark.parametrize(
    ("file", "content", "expected"),
    [
        ("pyproject", None, "cannot read"),
        ("pyproject", "[project\nversion = 1", "not valid TOML"),
        ("pyproject", '[project]\nname = "sre-tab"\ndynamic = ["version"]\n', "no static"),
        ("pyproject", '[tool.other]\nversion = "1.2.0"\n', "no static"),
        ("pyproject", '[project]\nversion = "1.2"\n', "no release tag can match"),
        ("pyproject", '[project]\nversion = "1.2.0.post1"\n', "no release tag can match"),
        ("pyproject", '[project]\nversion = "1.2.0+local"\n', "no release tag can match"),
        ("package_json", None, "cannot read"),
        ("package_json", "{not json", "not valid JSON"),
        ("package_json", '{"name": "frontend"}', "has no version"),
        ("package_json", '["1.2.0"]', "has no version"),
        ("package_json", '{"version": "1.2.0+build.5"}', "not MAJOR.MINOR.PATCH semver"),
        ("package_json", '{"version": "v1.2.0"}', "not MAJOR.MINOR.PATCH semver"),
    ],
)
def test_a_manifest_with_no_usable_version_is_refused(
    file: str, content: str | None, expected: str, tmp_path: Path
) -> None:
    """Missing or unreadable is a refusal, never a pass: an absent manifest
    must not read as one that agrees."""
    pyproject, package_json = manifests(tmp_path / "m", python="1.2.0", npm="1.2.0")
    target = pyproject if file == "pyproject" else package_json
    if content is None:
        target.unlink()
    else:
        target.write_text(content)

    result = resolve(
        "v1.2.0",
        _changelog_for(tmp_path, "1.2.0"),
        pyproject=pyproject,
        package_json=package_json,
    )

    assert result.returncode != 0
    assert expected in result.stderr
    assert str(target) in result.stderr


def _repository_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert isinstance(version, str)
    return version


def test_the_defaults_are_this_repositorys_manifests(tmp_path: Path) -> None:
    """Run from the checkout root with no manifest arguments, exactly as the
    workflow runs it, the resolver accepts this repository's own version and
    refuses another one. The second half is what shows the defaults are
    read rather than skipped.

    The changelog is a scratch one, so a version bump that has not yet
    written its release notes does not fail this for the wrong reason."""
    version = _repository_version()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"# Changelog\n\n## [{version}]\n\n- Now.\n\n## [99.0.0]\n\n- Later.\n")
    argv = [sys.executable, str(SCRIPT), "--changelog", str(changelog)]

    current = subprocess.run(
        [*argv, "--tag", f"v{version}"], capture_output=True, text=True, check=False, cwd=ROOT
    )
    other = subprocess.run(
        [*argv, "--tag", "v99.0.0"], capture_output=True, text=True, check=False, cwd=ROOT
    )

    assert current.returncode == 0, current.stderr
    assert other.returncode != 0
    assert "pyproject.toml says" in other.stderr
    assert "frontend/package.json says" in other.stderr


def test_the_defaults_refuse_a_checkout_with_no_manifests(tmp_path: Path) -> None:
    """Run from anywhere else, the defaults name files that are not there,
    and that is a refusal."""
    changelog = _changelog_for(tmp_path, "1.2.0")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", "v1.2.0", "--changelog", str(changelog)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "cannot read pyproject.toml" in result.stderr
