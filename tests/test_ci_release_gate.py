"""Tree-green release gate: same code is enough; SHA identity is not required."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from ci_release_gate import (  # noqa: E402
    _flatten_installer_files,
    filter_successful_runs,
    find_mac_update_zip,
    linux_release_has_appimage,
    matching_green_run,
    matching_installer_run,
    parse_mac_codesign_dump,
)


def _tree_map(mapping):
    def tree_for(sha):
        return mapping.get(sha)

    return tree_for


def test_matching_green_run_accepts_same_tree_from_pr_head():
    target = "tree-dest"
    runs = [
        {"headSha": "dest-tip", "url": "https://example/pr", "conclusion": "success"},
        {"headSha": "other", "url": "https://example/other", "conclusion": "success"},
    ]
    match = matching_green_run(
        target,
        runs,
        _tree_map({"dest-tip": "tree-dest", "other": "tree-other"}),
    )
    assert match is not None
    assert match["headSha"] == "dest-tip"


def test_matching_green_run_rejects_different_tree():
    match = matching_green_run(
        "tree-main-conflict",
        [{"headSha": "dest-tip", "conclusion": "success"}],
        _tree_map({"dest-tip": "tree-dest"}),
    )
    assert match is None


def test_matching_green_run_skips_non_success():
    match = matching_green_run(
        "tree-a",
        [{"headSha": "sha-a", "conclusion": "failure"}],
        _tree_map({"sha-a": "tree-a"}),
    )
    assert match is None


def test_matching_green_run_empty_tree_is_fail_closed():
    match = matching_green_run(
        "",
        [{"headSha": "sha-a", "conclusion": "success"}],
        _tree_map({"sha-a": ""}),
    )
    assert match is None


def test_matching_installer_run_requires_platform_artifact_and_skips_self():
    runs = [
        {
            "databaseId": 11,
            "headSha": "dest-tip",
            "conclusion": "success",
        },
        {
            "databaseId": 22,
            "headSha": "dest-tip",
            "conclusion": "success",
        },
    ]
    artifacts = {
        11: ["installer-mac", "installer-win", "installer-linux"],
        22: ["installer-mac"],
    }
    match = matching_installer_run(
        "tree-dest",
        runs,
        _tree_map({"dest-tip": "tree-dest"}),
        lambda run: artifacts[run["databaseId"]],
        "win",
        skip_run_ids=[11],
    )
    assert match is None

    match = matching_installer_run(
        "tree-dest",
        runs,
        _tree_map({"dest-tip": "tree-dest"}),
        lambda run: artifacts[run["databaseId"]],
        "mac",
        skip_run_ids=[99],
    )
    assert match is not None
    assert match["databaseId"] == 11


def test_flatten_installer_files_lifts_nested_artifact_layout(tmp_path):
    nested = tmp_path / "dl" / "webapp" / "release"
    nested.mkdir(parents=True)
    (nested / "latest-mac.yml").write_text("path: Marionette.dmg\n")
    (nested / "Marionette.dmg").write_bytes(b"dmg")
    (nested / "notes.txt").write_text("ignore")
    dest = tmp_path / "out"
    dest.mkdir()
    moved = _flatten_installer_files(str(tmp_path / "dl"), str(dest))
    names = sorted(Path(path).name for path in moved)
    assert names == ["Marionette.dmg", "latest-mac.yml"]
    assert (dest / "latest-mac.yml").is_file()


def test_filter_successful_runs_keeps_only_conclusion_success():
    runs = [
        {"headSha": "dest-tip", "conclusion": "success"},
        {"headSha": "in-flight", "conclusion": "", "status": "in_progress"},
        {"headSha": "failed", "conclusion": "failure"},
    ]
    kept = filter_successful_runs(runs)
    assert [run["headSha"] for run in kept] == ["dest-tip"]


def test_linux_release_has_appimage(tmp_path):
    (tmp_path / "latest-linux.yml").write_text("path: x.AppImage\n")
    assert linux_release_has_appimage(str(tmp_path)) is False
    (tmp_path / "Marionette-0.9.253.AppImage").write_bytes(b"app")
    assert linux_release_has_appimage(str(tmp_path)) is True


def test_parse_mac_codesign_dump_accepts_developer_id():
    dump = """
Identifier=com.marionette.app
Format=app bundle with Mach-O universal (x86_64 arm64)
Authority=Developer ID Application: Cary Palmer (ZDSDN9VC8M)
Authority=Developer ID Certification Authority
TeamIdentifier=ZDSDN9VC8M
"""
    parsed = parse_mac_codesign_dump(dump)
    assert parsed["ok"] is True
    assert parsed["team"] == "ZDSDN9VC8M"
    assert parsed["adhoc"] is False


def test_parse_mac_codesign_dump_rejects_adhoc_electron_identity():
    dump = """
Identifier=Electron
Format=app bundle with Mach-O universal (x86_64 arm64)
Signature=adhoc
TeamIdentifier=not set
"""
    parsed = parse_mac_codesign_dump(dump)
    assert parsed["ok"] is False
    assert parsed["adhoc"] is True
    assert parsed["identifier"] == "Electron"


def test_find_mac_update_zip_prefers_electron_builder_name(tmp_path):
    (tmp_path / "Marionette-0.9.251-universal-mac.zip").write_bytes(b"zip")
    (tmp_path / "notes.txt").write_text("ignore")
    found = find_mac_update_zip(str(tmp_path))
    assert found is not None
    assert found.endswith("Marionette-0.9.251-universal-mac.zip")


def test_release_yml_does_not_rerun_pytest():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "python -m pytest" not in text
    assert "test-gate" not in text
    assert "tests-already-green" in text
    assert "needs: [tests-already-green, build]" in text
    assert "puppetmaster-ai==" in text
    assert "fetch-depth: 0" in text
    assert "CSC_FOR_PULL_REQUEST" in text
    assert "require-mac-signature" in text


def test_tests_yml_windows_runner_is_swappable():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    assert "vars.CI_WINDOWS_RUNNER" in text
    assert "windows-latest" in text


def test_tests_yml_is_the_fast_dest_into_main_gate():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    assert "pytest-linux" in text
    assert "pytest-windows" in text
    assert "-n 4" in text
    assert "--dist loadscope" in text
    assert "PYTEST_SHARD" in text
    assert "python-version: \"3.9\"" in text
    assert "macos-latest" not in text
    full = (ROOT / ".github" / "workflows" / "tests-full.yml").read_text()
    assert "--resource-soak" in full
    assert "macos-latest" in full
