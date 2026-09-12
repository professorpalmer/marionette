from __future__ import annotations

import os

from harness.operator_read_roots import (
    collect_operator_read_roots,
    extract_named_paths,
    resolve_existing_root,
)


def test_extract_named_paths_tilde_and_users():
    text = (
        "Check ~/Downloads/visitor-access-poc-kit and "
        "/Users/carypalmer/Projects/ori-canaries please."
    )
    found = extract_named_paths(text)
    assert any(p.endswith("visitor-access-poc-kit") for p in found)
    assert any(p.endswith("ori-canaries") for p in found)


def test_recents_outside_tmp_are_readable_roots(tmp_path):
    extra = tmp_path / "Downloads" / "kit"
    extra.mkdir(parents=True)
    (extra / "note.md").write_text("hi\n", encoding="utf-8")
    roots = collect_operator_read_roots(
        "no paths here",
        recents=[str(extra)],
        home=str(tmp_path / "not-home"),
    )
    assert os.path.realpath(str(extra)) in [os.path.realpath(r) for r in roots]


def test_named_path_must_live_under_home(tmp_path):
    outside = tmp_path / "escape"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    roots = collect_operator_read_roots(
        f"read {outside}",
        recents=[],
        home=str(home),
    )
    assert roots == []


def test_ssh_dir_is_denied(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("key\n", encoding="utf-8")
    assert resolve_existing_root(str(ssh), home=str(tmp_path), require_home=True) is None
