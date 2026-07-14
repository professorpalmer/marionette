"""Unit tests for the pure intent contract -- no Puppetmaster needed."""
import pytest
from pmharness.intent import (
    validate_intent, parse_intent_text, IntentError, DriverIntent, KNOWN_ROLES,
    infer_roles, ROLE_LENSES,
)


def test_infer_roles_broad_audit_fans_out():
    # A broad/audit goal should fan out across every analysis lens, not run solo.
    for goal in (
        "Audit the platform for quality, robustness, and scale",
        "Review the codebase and find ways to make it better",
        "Do a comprehensive assessment of the whole system",
    ):
        roles = infer_roles(goal)
        assert len(roles) == 5
        assert set(roles) == set(KNOWN_ROLES)


def test_infer_roles_flow_goal_adds_pipeline():
    roles = infer_roles("Trace the end-to-end request flow")
    assert roles == ["explore", "pipeline-mapper"]


def test_infer_roles_narrow_goal_is_single_explorer():
    assert infer_roles("Where is the login handler defined?") == ["explore"]
    assert infer_roles("") == ["explore"]


def test_infer_roles_broadened_natural_language_fans_out():
    # Common "look at the whole thing" phrasings that previously collapsed to a
    # lone explorer must now fan out across every lens.
    for goal in (
        "look through the codebase for bugs",
        "find all the dead code and slop",
        "sweep the code base for smells",
        "find signs of vibe code everywhere",
    ):
        roles = infer_roles(goal)
        assert len(roles) == 5, goal
        assert set(roles) == set(KNOWN_ROLES), goal


def test_infer_roles_subsystem_goal_gets_focused_team():
    # A goal spanning a whole area (but not a full audit) gets a focused
    # multi-lens team, not a single explorer.
    for goal in (
        "how does the worker system work",
        "understand the queue layer",
        "investigate the swarm dispatch module",
        "walk me through the architecture",
    ):
        roles = infer_roles(goal)
        assert roles == ["explore", "conflict-auditor", "pipeline-mapper"], goal


def test_infer_roles_pinpoint_stays_single_even_with_area_word():
    # A pinpoint lookup that happens to mention a subsystem word must stay one
    # explorer -- pinpoint beats breadth.
    assert infer_roles("where is the queue system defined") == ["explore"]
    assert infer_roles("which file has the worker module") == ["explore"]


def test_infer_roles_locator_beats_broad_word():
    # A locator query must stay a single explorer even when it contains a broad
    # word like "find all" -- pinpoint precedes the audit fan-out.
    assert infer_roles("find all callers of send()") == ["explore"]
    assert infer_roles("who calls enqueue_prompt") == ["explore"]
    assert infer_roles("usages of ROLE_LENSES across the codebase") == ["explore"]


def test_role_lenses_cover_every_known_role():
    # Every role must carry a distinct lens so multi-role swarms don't duplicate.
    assert set(ROLE_LENSES) == set(KNOWN_ROLES)
    assert len(set(ROLE_LENSES.values())) == len(KNOWN_ROLES)


def test_valid_run_swarm():
    i = validate_intent({"action": "run_swarm", "goal": "audit repo"})
    assert i.action == "run_swarm" and i.goal == "audit repo"
    assert i.worker_mode == "subprocess"


def test_valid_run_prewalk():
    i = validate_intent({
        "action": "run_prewalk",
        "goal": "plan then implement the cache fix",
    })
    assert i.action == "run_prewalk"
    assert i.goal == "plan then implement the cache fix"


def test_run_prewalk_requires_goal():
    with pytest.raises(IntentError):
        validate_intent({"action": "run_prewalk"})


def test_is_prewalk_goal_detects_plan_then_implement():
    from pmharness.intent import is_prewalk_goal, classify_dispatch_action

    for goal in (
        "prewalk: add a settings toggle",
        "Plan then implement the retry helper",
        "Please plan-then-cheap the auth refactor",
        "plan and implement the logging cleanup",
        "quality plan then cheap implement of the parser",
    ):
        assert is_prewalk_goal(goal), goal
        assert classify_dispatch_action(goal) == "run_prewalk", goal


def test_is_prewalk_goal_rejects_ordinary_asks():
    from pmharness.intent import is_prewalk_goal, classify_dispatch_action

    for goal in (
        "Audit the platform for quality",
        "Where is the login handler defined?",
        "Implement the cache fix",
        "",
    ):
        assert not is_prewalk_goal(goal), goal
    assert classify_dispatch_action("Audit the platform") == "run_swarm"
    assert classify_dispatch_action("Implement the cache fix") == "run_swarm"
    assert classify_dispatch_action("") is None


def test_run_swarm_requires_goal():
    with pytest.raises(IntentError):
        validate_intent({"action": "run_swarm"})


def test_answer_and_stop_need_no_goal():
    assert validate_intent({"action": "answer"}).action == "answer"
    assert validate_intent({"action": "stop"}).action == "stop"


def test_bad_action():
    with pytest.raises(IntentError):
        validate_intent({"action": "explode"})


def test_bad_worker_mode():
    with pytest.raises(IntentError):
        validate_intent({"action": "run_swarm", "goal": "x", "worker_mode": "rocket"})


def test_unknown_role_rejected():
    with pytest.raises(IntentError):
        validate_intent({"action": "run_swarm", "goal": "x", "roles": ["nope"]})


def test_known_roles_pass():
    i = validate_intent({"action": "run_swarm", "goal": "x", "roles": list(KNOWN_ROLES)})
    assert i.roles == list(KNOWN_ROLES)


def test_parse_fenced_json():
    txt = "Here you go:\n```json\n{\"action\": \"answer\"}\n```\nDone."
    assert parse_intent_text(txt)["action"] == "answer"


def test_parse_bare_json_with_prose():
    txt = 'I think: {"action":"stop","rationale":"done"} ok'
    assert parse_intent_text(txt)["action"] == "stop"


def test_parse_nested_braces():
    txt = '{"action":"run_swarm","goal":"x","raw":{"a":{"b":1}}}'
    assert parse_intent_text(txt)["goal"] == "x"


def test_parse_no_json_raises():
    with pytest.raises(IntentError):
        parse_intent_text("no json here at all")


def test_validate_from_text():
    i = validate_intent('{"action":"answer"}')
    assert i.action == "answer"
