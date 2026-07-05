"""Shared, propagating budget across the pilot->swarm->worker spawn tree.

A deep spawn tree must NOT reset the ceiling per level. These tests prove the
single-shared-decrementing-budget design: a child budget's spend rolls up into
its parent, a shared budget threaded through two sequential workers accumulates
across BOTH (so the second sees the first's spend and check() halts when the
shared ceiling is crossed), and supervised mode (no governing budget) still
hands a worker its own independent default.
"""

import os
import shutil
import subprocess
import tempfile

import pytest

from harness.autobudget import AutoBudget
from harness.worker import (
    ProviderWorker,
    ambient_budget,
    get_ambient_budget,
    set_ambient_budget,
)


def _make_repo():
    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
    with open(os.path.join(repo, "f.txt"), "w") as fh:
        fh.write("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# (1) child spend rolls up into the parent
# ---------------------------------------------------------------------------

def test_child_add_tokens_raises_parent_tokens_used():
    parent = AutoBudget(max_tokens=1000).start()
    child = parent.child()
    assert child.tokens_used == 0
    assert parent.tokens_used == 0

    child.add_tokens(250)
    # The child's spend is visible on the SHARED (parent) ceiling.
    assert child.tokens_used == 250
    assert parent.tokens_used == 250


def test_child_add_swarm_raises_parent_swarms_used():
    parent = AutoBudget(max_swarms=5).start()
    child = parent.child()
    child.add_swarm()
    child.add_swarm()
    assert parent.swarms_used == 2
    assert child.swarms_used == 2


def test_child_inherits_ceilings_and_current_totals():
    parent = AutoBudget(max_tokens=999, max_swarms=7, killswitch_path="/nope").start()
    parent.add_tokens(400)
    parent.add_swarm()
    child = parent.child()
    # Child inherits the ceilings...
    assert child.max_tokens == 999
    assert child.max_swarms == 7
    assert child.killswitch_path == "/nope"
    # ...and the tree's CURRENT position (spend that happened before it existed).
    assert child.tokens_used == 400
    assert child.swarms_used == 1


def test_grandchild_rolls_all_the_way_up():
    root = AutoBudget(max_tokens=10_000).start()
    mid = root.child()
    leaf = mid.child()
    leaf.add_tokens(123)
    assert leaf.tokens_used == 123
    assert mid.tokens_used == 123
    assert root.tokens_used == 123


# ---------------------------------------------------------------------------
# (2) shared budget threaded to two sequential workers accumulates across BOTH
# ---------------------------------------------------------------------------

def test_two_sequential_children_accumulate_and_halt_on_shared_ceiling():
    # One governing ceiling for the whole spawn tree.
    governor = AutoBudget(max_tokens=1000, max_swarms=99, max_idle_steps=99).start()

    # First worker (a child()) spends most of the budget.
    w1 = governor.child()
    w1.add_tokens(700)
    assert governor.tokens_used == 700
    assert w1.check() is None  # 700 < 1000, still fine

    # Second, SEQUENTIAL worker gets a fresh child() -- but it sees the first
    # worker's spend because the ceiling is shared, not reset per level.
    w2 = governor.child()
    assert w2.tokens_used == 700  # inherits the tree's current total
    w2.add_tokens(400)            # pushes cumulative to 1100 >= 1000
    assert governor.tokens_used == 1100

    # The shared ceiling is now crossed: the second worker's check() halts, and
    # so does the governor's -- the tree-wide cap actually caps the tree.
    assert w2.check() is not None
    assert "token ceiling" in w2.check()
    assert governor.check() is not None


def test_child_honours_parent_already_tripped_halt():
    governor = AutoBudget(max_tokens=100).start()
    governor.add_tokens(150)
    assert governor.check() is not None  # parent trips first
    child = governor.child()
    # Even a brand-new child inherits the tripped halt of the shared ceiling.
    assert child.check() is not None


def test_swarm_ceiling_caps_across_children():
    governor = AutoBudget(max_tokens=10_000, max_swarms=3, max_idle_steps=99).start()
    a = governor.child()
    a.add_swarm()
    a.add_swarm()
    b = governor.child()
    assert b.swarms_used == 2
    b.add_swarm()  # third swarm across the shared tree -> at ceiling
    assert governor.swarms_used == 3
    assert b.check() is not None
    assert "swarm ceiling" in b.check()


# ---------------------------------------------------------------------------
# (3) ambient budget threading into ProviderWorker; supervised stays independent
# ---------------------------------------------------------------------------

def test_worker_adopts_ambient_governing_budget_as_child():
    repo = _make_repo()
    try:
        governor = AutoBudget(max_tokens=5000, max_swarms=9).start()
        governor.add_tokens(120)
        with ambient_budget(governor):
            worker = ProviderWorker(repo, "do a thing")
            # The worker did NOT mint a fresh default: it bound a child() of the
            # governing budget, sharing the ONE ceiling and the current total.
            assert worker.budget.parent is governor
            assert worker.budget.max_tokens == 5000
            assert worker.budget.tokens_used == 120
            # Its spend rolls up into the shared ceiling.
            worker.budget.add_tokens(80)
            assert governor.tokens_used == 200
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_supervised_worker_gets_independent_default_budget():
    repo = _make_repo()
    try:
        # No ambient/governing budget installed (supervised mode).
        assert get_ambient_budget() is None
        worker = ProviderWorker(repo, "do a thing")
        # Preserves today's behavior: a fresh, per-worker default budget with no
        # parent -- so a supervised run is not regressed onto some shared ceiling.
        assert worker.budget.parent is None
        assert worker.budget.max_tokens == 40000
        assert worker.budget.max_swarms == 2
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_explicit_budget_used_when_no_ambient():
    repo = _make_repo()
    try:
        assert get_ambient_budget() is None
        mine = AutoBudget(max_tokens=1234)
        worker = ProviderWorker(repo, "goal", budget=mine)
        assert worker.budget is mine
        assert worker.budget.parent is None
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_ambient_budget_context_restores_previous():
    assert get_ambient_budget() is None
    outer = AutoBudget()
    with ambient_budget(outer):
        assert get_ambient_budget() is outer
        inner = AutoBudget()
        with ambient_budget(inner):
            assert get_ambient_budget() is inner
        assert get_ambient_budget() is outer
    assert get_ambient_budget() is None


def test_set_ambient_budget_returns_previous():
    assert get_ambient_budget() is None
    b = AutoBudget()
    prev = set_ambient_budget(b)
    try:
        assert prev is None
        assert get_ambient_budget() is b
    finally:
        set_ambient_budget(prev)
    assert get_ambient_budget() is None
