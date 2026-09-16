"""Tree-green release gate: same code is enough; SHA identity is not required."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_release_gate as gate  # noqa: E402
from ci_release_gate import (  # noqa: E402
    _flatten_installer_files,
    cmd_skip_if_green,
    filter_successful_runs,
    find_mac_update_zip,
    linux_release_has_appimage,
    matching_green_run,
    matching_installer_run,
    parse_mac_codesign_dump,
    should_skip_push_suite,
    write_github_output,
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


def test_should_skip_push_suite_only_on_push_with_other_green_tree():
    runs = [
        {"headSha": "pr-head", "databaseId": 11, "conclusion": "success"},
        {"headSha": "this-push", "databaseId": 22, "conclusion": "success"},
    ]
    trees = {"pr-head": "tree-dest", "this-push": "tree-dest"}
    assert should_skip_push_suite(
        "pull_request", "tree-dest", runs, _tree_map(trees), current_run_id=22
    ) is False
    assert should_skip_push_suite(
        "push", "tree-dest", runs, _tree_map(trees), current_run_id=22
    ) is True
    assert should_skip_push_suite(
        "push", "tree-conflict", runs, _tree_map(trees), current_run_id=22
    ) is False
    assert should_skip_push_suite(
        "push",
        "tree-dest",
        [{"headSha": "this-push", "databaseId": 22, "conclusion": "success"}],
        _tree_map(trees),
        current_run_id=22,
    ) is False
    assert should_skip_push_suite(
        "push",
        "tree-dest",
        [{"headSha": "pr-head", "databaseId": 11, "conclusion": "failure"}],
        _tree_map(trees),
        current_run_id=22,
    ) is False


def test_cmd_skip_if_green_lookup_failure_runs_suite(monkeypatch, tmp_path):
    dest = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(dest))
    monkeypatch.setattr(gate, "git_tree_sha", lambda rev="HEAD": "tree-dest")

    def boom(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["gh", "run", "list"])

    monkeypatch.setattr(gate, "_list_workflow_runs", boom)
    args = argparse.Namespace(
        event="push", repo="owner/repo", run_id="99", sha="HEAD", limit=80
    )
    assert cmd_skip_if_green(args) == 0
    assert dest.read_text(encoding="utf-8") == "skip_suite=false\n"


def test_write_github_output_appends_when_path_given(tmp_path):
    dest = tmp_path / "output"
    write_github_output("skip_suite", "true", path=str(dest))
    write_github_output("skip_suite", "false", path=str(dest))
    assert dest.read_text(encoding="utf-8") == "skip_suite=true\nskip_suite=false\n"


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
    # Do not default the job to a third-party label. Blacksmith is org-only
    # and an unset/missing runner queues the gate forever.
    assert "runs-on: blacksmith-" not in text
    assert "runs-on: ${{ vars.CI_WINDOWS_RUNNER || 'windows-latest' }}" in text


def test_tests_yml_is_the_fast_dest_into_main_gate():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    assert "pytest-linux" in text
    assert "pytest-windows" in text
    assert "-n 4" in text
    assert "--dist loadscope" in text
    assert "PYTEST_SHARD" in text
    lin_start = text.index("  pytest-linux:")
    lin_end = text.index("\n  pytest-windows:", lin_start)
    assert 'python-version: "3.9"' in text[lin_start:lin_end]
    mac_start = text.index("  pytest-macos:")
    mac_end = text.index("\n  frontend-build:", mac_start)
    mac_job = text[mac_start:mac_end]
    assert "runs-on: macos-latest" in mac_job
    assert 'python-version: "3.11"' in mac_job
    assert '"puppetmaster-ai==1.27.25"' in mac_job
    assert "run: python -m pytest -q -p no:cacheprovider -n 4 --dist loadscope" in mac_job
    assert "continue-on-error" not in mac_job
    assert "needs: reuse-green-tree" in mac_job
    assert "needs.reuse-green-tree.outputs.skip_suite != 'true'" in mac_job
    assert "reuse-green-tree" in text
    assert "skip-if-green" in text
    assert "skip_suite" in text
    assert "needs: reuse-green-tree" in text
    assert "needs.reuse-green-tree.outputs.skip_suite != 'true'" in text
    full = (ROOT / ".github" / "workflows" / "tests-full.yml").read_text()
    assert "--resource-soak" in full
    assert "macos-latest" in full


def test_tests_yml_requires_both_windows_interpreters():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    start = text.index("  pytest-windows:")
    end = text.index("\n  pytest-macos:", start)
    job = text[start:end]
    assert 'python-version: ["3.9", "3.11"]' in job
    assert 'name: pytest-windows (${{ matrix.python-version }}, ${{ matrix.shard }})' in job
    assert "python-version: ${{ matrix.python-version }}" in job
    assert "shard: [1, 2, 3, 4]" in job
    assert "PYTEST_SHARD: ${{ matrix.shard }}/4" in job
    assert "continue-on-error" not in job
    assert "vars.CI_WINDOWS_RUNNER" in job
