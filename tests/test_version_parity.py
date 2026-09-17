"""One version, written in four places, which must agree.

``pyproject.toml`` is the authority: it is what the installed distribution
reports, and so what ``app/main.py`` publishes as the OpenAPI version. The
page footer reads ``frontend/package.json`` instead, because the frontend
stage of the image build only has the frontend tree. A release that bumps
one and forgets the other ships a footer naming the previous version, with
every other check still green.

Compared as PEP 440 versions rather than as strings, so a pre-release spelt
``1.2.0rc1`` in Python and ``1.2.0-rc.1`` for npm counts as one version.
This runs on every push, including the push of a release tag, whose publish
job waits on this suite. That job's release resolver then compares
``pyproject.toml`` and ``package.json`` with the tag itself, which covers
the release where all four were left at the previous version.
"""

from __future__ import annotations

import json
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path

# Not a declared dependency: pytest requires it, and this module only ever
# runs under pytest.
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> Version:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return Version(tomllib.load(handle)["project"]["version"])


def _json(relative: str) -> dict[str, object]:
    loaded = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_installed_distribution_is_the_pyproject_version() -> None:
    """What the API reports. A stale install would make the comparisons
    below agree with a file the running application does not."""
    assert Version(installed_version("sre-tab")) == _pyproject_version()


def test_the_frontend_manifest_carries_the_application_version() -> None:
    assert Version(str(_json("frontend/package.json")["version"])) == _pyproject_version()


def test_the_frontend_lockfile_carries_the_application_version() -> None:
    """``npm ci`` does not rewrite the lockfile, so a bump made by hand in
    ``package.json`` leaves the old version here unless ``npm install``
    was run as well."""
    lock = _json("frontend/package-lock.json")
    packages = lock["packages"]
    assert isinstance(packages, dict)
    assert Version(str(lock["version"])) == _pyproject_version()
    assert Version(str(packages[""]["version"])) == _pyproject_version()
