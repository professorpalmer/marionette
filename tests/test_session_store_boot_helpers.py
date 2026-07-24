"""Direct coverage for SessionStore boot-migration helpers."""

from __future__ import annotations

from harness.sessions import SessionStore


def test_remove_rows_deletes_ids_and_promotes_same_workspace(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    keep_a = store.create("keep-a", repo=str(repo_a), workspace_root=str(repo_a))
    store.create("drop-b", repo=str(repo_b), workspace_root=str(repo_b))
    active_a = store.create("active-a", repo=str(repo_a), workspace_root=str(repo_a))
    assert store.active == active_a["id"]

    removed = store.remove_rows([active_a["id"], "missing-id"])
    assert removed == [active_a["id"]]
    ids = {s["id"] for s in store.rows()}
    assert active_a["id"] not in ids
    assert keep_a["id"] in ids
    # Active was removed → promote same-workspace sibling, not repo_b.
    assert store.active == keep_a["id"]


def test_activate_newest_for_root_scopes_to_workspace(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    older_a = store.create("A-old", repo=str(repo_a), workspace_root=str(repo_a))
    b = store.create("B", repo=str(repo_b), workspace_root=str(repo_b))
    newer_a = store.create("A-new", repo=str(repo_a), workspace_root=str(repo_a))
    for row in store._sessions:
        if row["id"] == older_a["id"]:
            row["created"] = 1.0
        elif row["id"] == newer_a["id"]:
            row["created"] = 3.0
        elif row["id"] == b["id"]:
            row["created"] = 99.0

    # Point active at B first so promotion must move within A only.
    store._active = b["id"]
    store._save(immediate=True)

    got = store.activate_newest_for_root(str(repo_a))
    assert got == newer_a["id"]
    assert store.active == newer_a["id"]

    # Unknown root → no active (never yank to B).
    store._active = newer_a["id"]
    store._save(immediate=True)
    assert store.activate_newest_for_root(str(tmp_path / "missing")) is None
