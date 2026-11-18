"""Regression tests for the pilot_guards classifier and loop-key fixes."""
from __future__ import annotations

import pytest

from harness.pilot import PilotAction
from harness.pilot_guards import (
    is_broad_intent_user_message,
    is_explicit_swarm_user_message,
    normalize_action_args,
    swarm_policy_for_message,
    swarm_policy_table,
)


# Bug 1af9b7d4-1: a narrow question in another clause cancelled a launch.
def test_launch_clause_survives_separate_where_is_question():
    msg = "Launch a swarm to audit the repo. Where is the config?"
    assert is_explicit_swarm_user_message(msg) is True


def test_where_is_question_alone_stays_not_explicit():
    assert is_explicit_swarm_user_message("Where is run_swarm defined?") is False
    assert is_explicit_swarm_user_message("Where is the config? Run it.") is False


# Bug 1af9b7d4-3: a typographic apostrophe hid the refusal.
@pytest.mark.parametrize("msg", [
    "Don’t swarm the repo",
    "don’t use a swarm",
])
def test_typographic_apostrophe_refusal(msg):
    assert is_explicit_swarm_user_message(msg) is False
    assert swarm_policy_for_message(msg) == "solo"


def test_plain_swarm_the_repo_stays_explicit():
    assert is_explicit_swarm_user_message("please swarm the repo") is True
    assert is_explicit_swarm_user_message("Don't swarm the repo") is False


# Bug e3af4031-2: search_files keys ignored "paths" and result options.
def _search(**arguments):
    return PilotAction(kind="search_files", query="foo", arguments={"query": "foo", **arguments})


def test_search_files_paths_scope_is_in_key():
    a = normalize_action_args("search_files", _search(paths=["src"]))
    b = normalize_action_args("search_files", _search(paths=["tests"]))
    assert a != b


@pytest.mark.parametrize("extra", [
    {"glob": "*.py"},
    {"case_insensitive": True},
    {"context_lines": 2},
    {"files_only": True},
])
def test_search_files_options_are_in_key(extra):
    assert (
        normalize_action_args("search_files", _search(**extra))
        != normalize_action_args("search_files", _search())
    )


def test_search_files_same_scope_still_dedupes():
    a = normalize_action_args("search_files", _search(paths=["src", "tests"]))
    b = normalize_action_args("search_files", _search(paths=["tests", "src"]))
    assert a == b
    plain = normalize_action_args("search_files", _search(path="src"))
    assert plain == (
        '{"kind":"search_files","max_results":null,"path":"src","query":"foo"}'
    )


# Bug e3af4031-5: edit_file collapsed whitespace in old_str/new_str.
def test_edit_file_whitespace_difference_gives_distinct_keys():
    a = PilotAction(kind="edit_file", path="m.py", old_str="x  = 1", new_str="x = 2")
    b = PilotAction(kind="edit_file", path="m.py", old_str="x = 1", new_str="x = 2")
    assert normalize_action_args("edit_file", a) != normalize_action_args("edit_file", b)
    c = PilotAction(kind="edit_file", path="m.py", old_str="    y = 1", new_str="y")
    d = PilotAction(kind="edit_file", path="m.py", old_str="y = 1", new_str="y")
    assert normalize_action_args("edit_file", c) != normalize_action_args("edit_file", d)


def test_edit_file_identical_edit_still_dedupes():
    a = PilotAction(kind="edit_file", path="m.py", old_str="x = 1", new_str="x = 2")
    b = PilotAction(kind="edit_file", path="M.py", old_str="x = 1", new_str="x = 2")
    assert normalize_action_args("edit_file", a) == normalize_action_args("edit_file", b)


# Bug 791b83f9-1: the narrow veto matched inside "everywhere is".
@pytest.mark.parametrize("msg", [
    "Audit the whole codebase, everywhere is a risk",
    "Audit the whole repo, somewhere is a memory leak.",
    "Audit the auth module and check whether anything elsewhere is affected.",
])
def test_broad_request_with_in_word_where_is(msg):
    assert is_broad_intent_user_message(msg) is True


def test_real_where_is_still_narrow():
    assert is_broad_intent_user_message("Audit it. Where is the config?") is False


# Bug 791b83f9-2: "map the" matched inside "bitmap the" / "roadmap the".
@pytest.mark.parametrize("msg", [
    "Create a bitmap the way the logo needs",
    "Rename the roadmap the team wrote to plan.md",
    "Write a roadmap the team can follow for next quarter.",
])
def test_in_word_map_the_is_not_broad(msg):
    assert is_broad_intent_user_message(msg) is False


def test_map_the_verb_stays_broad():
    assert is_broad_intent_user_message("Map the pipeline architecture") is True


# The owner's rules for 1af9b7d4-2, 1af9b7d4-5 and 791b83f9-7: a refusal
# always wins, a launch beats a narrow question, the Windows rule stays.
_GOLDEN = [
    # 1af9b7d4-2: a refusal of agentic workers.
    ("Please do not use agentic workers", "solo"),
    ("do not use agentic workers", "solo"),
    ("Don’t launch any agentic workers", "solo"),
    ("never spawn subagents for this", "solo"),
    ("no need to run a swarm", "solo"),
    # 1af9b7d4-5: a launch verb beats a narrow phrase in its clause.
    ("Launch a swarm to find where is the auth token validated", "explicit"),
    ("where is the swarm launched? spin up a swarm to check", "explicit"),
    # Counter-examples of the review packet: each keeps its result.
    ("Use a swarm to audit auth. Do not run more than three workers", "explicit"),
    ("Run a swarm over auth, do not use the old swarm results", "explicit"),
    ("Launch a swarm. Don't start until the swarm tracker is clear", "explicit"),
    ("Where is the code we run when the swarm finishes?", "solo"),
    ("Do not dispatch more than two workers", "solo"),
    ("Why does the build fail on Linux but not Windows?", "broad"),
    ("Does this work on Mac and Windows?", "broad"),
    # 791b83f9-7: accepted by the owner (low severity).
    ("How do I close all browser windows on my Mac?", "broad"),
    # Earlier rows of this file.
    ("Launch a swarm to audit the repo. Where is the config?", "explicit"),
    ("Where is the config? Run it.", "solo"),
    ("Don’t swarm the repo", "solo"),
    ("please swarm the repo", "explicit"),
    # The last delegation clause wins (tests/test_pilot_guards.py keeps the
    # contrast "no workers, then use a swarm").
    ("Use a swarm to audit everything. Actually, do not use the swarm", "solo"),
    ("Do not use workers for this part, but use a swarm for the audit.", "explicit"),
]


@pytest.mark.parametrize("msg,policy", _GOLDEN)
def test_swarm_policy_golden_table(msg, policy):
    assert swarm_policy_for_message(msg) == policy


@pytest.mark.parametrize("refusal", [False, True])
@pytest.mark.parametrize("launch", [False, True])
@pytest.mark.parametrize("narrow", [False, True])
@pytest.mark.parametrize("broad", [False, True])
def test_swarm_policy_table_order(refusal, launch, narrow, broad):
    if refusal:
        expected = "solo"
    elif launch:
        expected = "explicit"
    elif narrow:
        expected = "solo"
    elif broad:
        expected = "broad"
    else:
        expected = "solo"
    assert swarm_policy_table(refusal, launch, narrow, broad) == expected
