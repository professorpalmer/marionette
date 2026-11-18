from __future__ import annotations

import pytest

from harness.skill_store import Skill, SkillStore, _slug


def _rows(store):
    return sorted((k.name, k.state, k.body) for k in store.list())


# Bug 81081b2d-1: the 48-char cut left a trailing separator.

def test_slug_cut_on_separator_has_no_trailing_dash():
    name = "a" * 47 + " b"
    assert Skill(name=name).slug == "a" * 47
    assert _slug(_slug(name)) == _slug(name)


def test_slug_cut_on_letter_keeps_old_result():
    name = "a" * 46 + " bc"
    assert Skill(name=name).slug == "a" * 46 + "-b"


def test_cut_slug_save_round_trips_through_api_slug(tmp_path):
    store = SkillStore(str(tmp_path))
    sk = Skill(name="a" * 47 + " b", body="x", state="active")
    store.save(sk)
    got = store.get(sk.slug)
    assert got is not None and got.name == sk.name


# Bug f75dc534-1: a pending patch overwrote an unrelated "<slug>-patch" skill.

def test_propose_update_does_not_overwrite_unrelated_patch_slug(tmp_path):
    store = SkillStore(str(tmp_path))
    store.save(Skill(name="Deploy", body="a", state="active"))
    store.save(Skill(name="Deploy patch", body="unrelated", state="active"))
    with pytest.raises(ValueError):
        store.propose_update("deploy", "new")
    assert _rows(store) == [("Deploy", "active", "a"),
                            ("Deploy patch", "active", "unrelated")]


def test_propose_update_still_replaces_own_earlier_patch(tmp_path):
    store = SkillStore(str(tmp_path))
    store.save(Skill(name="Deploy", body="a", state="active"))
    store.propose_update("deploy", "v2")
    store.propose_update("deploy", "v3")
    assert _rows(store) == [("Deploy", "active", "a"), ("Deploy", "pending", "v3")]


# Bug f75dc534-2: approval of a renamed patch left the old active skill.

def test_approve_renamed_patch_replaces_root_skill(tmp_path):
    store = SkillStore(str(tmp_path))
    store.save(Skill(name="Deploy", body="a", state="active"))
    store.propose_update("deploy", "new", new_name="Ship")
    store.set_state("deploy-patch", "active")
    assert _rows(store) == [("Ship", "active", "new")]


def test_approve_patch_with_same_name_keeps_old_result(tmp_path):
    store = SkillStore(str(tmp_path))
    store.save(Skill(name="Deploy", body="a", state="active"))
    store.propose_update("deploy", "new")
    store.set_state("deploy-patch", "active")
    assert _rows(store) == [("Deploy", "active", "new")]
