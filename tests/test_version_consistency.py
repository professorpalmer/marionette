"""Version stamps must not drift.

Marionette is ONE product with three version stamps: the Electron shell
(webapp/package.json -- electron-builder names the DMG from it), the Python rig
(pyproject.toml), and harness.__version__. They drifted badly in the past
(package.json 0.6.44 vs pyproject 0.6.5 vs __init__ 0.1.0), so a released build
reported three different versions depending on where you looked. This guard fails
CI the moment they diverge, forcing every release to bump all three in lockstep.

stdlib-only on purpose (tomllib is 3.11+, CI also runs 3.9): the pyproject and
__init__ versions are read with a small regex rather than a TOML/AST parser."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _package_json_version() -> str:
    data = json.loads((ROOT / "webapp" / "package.json").read_text())
    return data["version"]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "no `version = \"...\"` found in pyproject.toml"
    return match.group(1)


def _init_version() -> str:
    text = (ROOT / "harness" / "__init__.py").read_text()
    match = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"', text)
    assert match, "no `__version__ = \"...\"` found in harness/__init__.py"
    return match.group(1)


def test_version_stamps_are_consistent():
    pkg = _package_json_version()
    pyproject = _pyproject_version()
    init = _init_version()
    assert pkg == pyproject == init, (
        f"version drift: webapp/package.json={pkg}  pyproject.toml={pyproject}  "
        f"harness.__version__={init} -- bump all three together for a release"
    )


def test_version_is_pep440_ish():
    """A sanity floor: the shared version looks like a release number, not a
    placeholder like 0.0.0 or a leftover 0.1.0."""
    v = _package_json_version()
    assert re.match(r"^\d+\.\d+\.\d+([.\-+].+)?$", v), f"unexpected version format: {v}"
