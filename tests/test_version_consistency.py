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


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _package_json_version() -> str:
    data = json.loads(_read("webapp/package.json"))
    return data["version"]


def _pyproject_version() -> str:
    text = _read("pyproject.toml")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "no `version = \"...\"` found in pyproject.toml"
    return match.group(1)


def _init_version() -> str:
    text = _read("harness/__init__.py")
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


def test_pyproject_declares_pinned_puppetmaster_runtime_dependency():
    """Standalone ``pip install pm-harness`` must own the Puppetmaster pin.

    Desktop/Electron also re-checks the pin, but a fresh isolated Python install
    must not succeed while leaving ``import puppetmaster`` broken.
    """
    text = _read("pyproject.toml")
    match = re.search(
        r'(?ms)^dependencies\s*=\s*\[(.*?)\]',
        text,
    )
    assert match, "no [project] dependencies = [...] in pyproject.toml"
    deps = match.group(1)
    pin = re.search(r'"puppetmaster-ai==(\d+\.\d+\.\d+)"', deps)
    assert pin, (
        "pyproject.toml runtime dependencies must pin puppetmaster-ai==X.Y.Z "
        "(standalone install ownership)"
    )
    assert pin.group(1), "empty Puppetmaster pin"


def test_puppetmaster_install_and_packaging_pins_match():
    expected = re.search(
        r'"(puppetmaster-ai==\d+\.\d+\.\d+)"',
        _read("pyproject.toml"),
    ).group(1)
    paths = (
        "scripts/install.sh", "scripts/install.ps1",
        "scripts/doctor.sh", "scripts/doctor.ps1",
        "webapp/electron/bootstrap.cjs", "webapp/electron/update-pm.cjs",
        "harness/diag_bundle.py",
        ".github/workflows/tests.yml", ".github/workflows/tests-full.yml",
        ".github/workflows/release.yml", "README.md", "CONTRIBUTING.md",
    )
    for path in paths:
        pins = re.findall(r"puppetmaster-ai==\d+\.\d+\.\d+", _read(path))
        assert pins, f"{path} has no Puppetmaster pin"
        assert set(pins) == {expected}, f"{path}: {pins} differs from {expected}"


def test_uv_lock_matches_project_version_and_puppetmaster_pin():
    packages = {}
    for block in re.split(r"(?m)^\[\[package\]\]\s*$", _read("uv.lock"))[1:]:
        name = re.search(r'(?m)^name = "([^"]+)"$', block)
        assert name, "uv.lock package has no name"
        packages.setdefault(name.group(1), []).append(block)
    assert len(packages.get("pm-harness", [])) == 1
    assert len(packages.get("puppetmaster-ai", [])) == 1
    project = packages["pm-harness"][0]
    runtime = packages["puppetmaster-ai"][0]
    pin = re.search(r'"puppetmaster-ai==([^" ]+)"', _read("pyproject.toml"))
    assert pin, "pyproject.toml has no Puppetmaster pin"
    version = re.search(r'(?m)^version = "([^"]+)"$', project)
    resolved = re.search(r'(?m)^version = "([^"]+)"$', runtime)
    metadata = project.split("[package.metadata]", 1)[1]
    requirement = re.search(
        r'\{ name = "puppetmaster-ai", specifier = "([^"]+)" \}', metadata,
    )
    assert version and resolved and requirement
    actual = (version.group(1), requirement.group(1), resolved.group(1))
    expected = (_pyproject_version(), "==" + pin.group(1), pin.group(1))
    assert actual == expected, f"uv.lock version/requirement/resolution drift: {actual} != {expected}"
