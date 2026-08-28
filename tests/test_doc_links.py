"""The documentation link gate has to go red, not merely exist.

`.github/scripts/check-doc-links.py` is the only check standing between a
reworded heading and a link that silently lands the reader at the top of a
30KB document. A gate like that is worth exactly as much as its red path,
and this repository has shipped six checks that reported success while
verifying nothing. So each failure mode is exercised here against a real
throwaway repository rather than trusted to read correctly.

The green path is covered too, and deliberately last in each case: a checker
that fails on everything is as useless as one that passes everything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check-doc-links.py"

GOOD = """<a id="anchored"></a>
## Anchored

Links: [self](#anchored), [across](sub/other.md#target), [plain](sub/other.md).

A fenced example, which declares nothing and links to nothing:

```markdown
<a id="anchored"></a>
[example](does/not/exist.md)
```

An inline span, likewise: `[example](also/missing.md)`
"""

OTHER = """<a id="target"></a>
## Target

Back to [a](../root.md#anchored).
"""


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    """A throwaway git repository holding two linked documents."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "root.md").write_text(GOOD)
    (tmp_path / "sub" / "other.md").write_text(OTHER)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT)],
        cwd=root,
        capture_output=True,
        text=True,
    )


def test_clean_documents_pass(docs: Path) -> None:
    result = check(docs)
    assert result.returncode == 0, result.stderr
    assert "all links resolve" in result.stdout


def test_fenced_and_inline_examples_are_not_checked(docs: Path) -> None:
    """Both example links above point at files that do not exist.

    They are syntax being shown, not references being made, and treating
    them as references produces a failure the author cannot fix without
    rewriting the sentence.
    """
    result = check(docs)
    assert result.returncode == 0
    assert "does/not/exist.md" not in result.stderr
    assert "also/missing.md" not in result.stderr


def test_fragment_with_no_declared_anchor_fails(docs: Path) -> None:
    (docs / "root.md").write_text(GOOD + "\nSee [nope](sub/other.md#undeclared).\n")
    result = check(docs)
    assert result.returncode == 1
    assert 'declares no <a id="undeclared"></a>' in result.stderr


def test_missing_file_fails(docs: Path) -> None:
    (docs / "root.md").write_text(GOOD + "\nSee [gone](sub/absent.md).\n")
    result = check(docs)
    assert result.returncode == 1
    assert "broken relative link: sub/absent.md" in result.stderr


def test_duplicate_anchor_id_fails(docs: Path) -> None:
    """Two identical ids mean a link that resolves somewhere arbitrary."""
    (docs / "sub" / "other.md").write_text(OTHER + '\n<a id="target"></a>\n## Again\n')
    result = check(docs)
    assert result.returncode == 1
    assert "duplicate anchor id 'target'" in result.stderr


def test_anchor_must_be_at_column_zero(docs: Path) -> None:
    """The fixed shape is the point: it is what keeps this greppable.

    An indented anchor is not a declaration, so the link naming it fails —
    which is the right direction. The alternative is accepting any shape and
    losing the property that one `sed` can undo the whole convention.
    """
    (docs / "sub" / "other.md").write_text(OTHER.replace("<a id=", "  <a id=", 1))
    result = check(docs)
    assert result.returncode == 1
    assert 'declares no <a id="target"></a>' in result.stderr


def test_nested_fence_of_greater_length_is_one_block(docs: Path) -> None:
    """A ````-delimited block may contain ``` examples.

    CONTRIBUTING.md has exactly this shape, documenting the `docs:run`
    marker. Toggling on every fence-looking line reads the inner examples as
    prose, so the link below would be checked and reported broken.
    """
    (docs / "root.md").write_text(
        GOOD + "\n````markdown\n```sh\n[inner](nowhere.md)\n```\n````\n",
    )
    result = check(docs)
    assert result.returncode == 0, result.stderr
    assert "nowhere.md" not in result.stderr


def test_indented_pseudo_fence_does_not_open_a_block(docs: Path) -> None:
    """Four spaces makes it an indented code block, not a fence.

    Treating it as a fence desynchronises the state and silently skips
    everything after it — a false pass, and the direction that matters.
    """
    (docs / "root.md").write_text(
        GOOD + "\n    ```\n\nAnd then [gone](sub/absent.md).\n",
    )
    result = check(docs)
    assert result.returncode == 1
    assert "broken relative link: sub/absent.md" in result.stderr


def test_detached_anchor_fails(docs: Path) -> None:
    """An anchor left behind when its heading moves still resolves.

    The browser scrolls to wherever the anchor sits, so every link to it
    keeps working and starts pointing at the wrong content — silent, which
    is exactly what this gate exists to catch.
    """
    (docs / "sub" / "other.md").write_text(
        '<a id="target"></a>\n\nSome prose in between.\n\n## Target\n',
    )
    result = check(docs)
    assert result.returncode == 1
    assert "is not immediately above a heading" in result.stderr


def test_parent_relative_paths_resolve(docs: Path) -> None:
    """`sub/other.md` links up to `../root.md#anchored`.

    Path normalisation has to collapse that to the repository-relative key
    the anchor table is built on, or every cross-directory link reports a
    missing anchor that is plainly there.
    """
    result = check(docs)
    assert result.returncode == 0
    assert "root.md" not in result.stderr
