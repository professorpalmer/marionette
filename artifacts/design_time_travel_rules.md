# Design: time-travel rules (turn-context journal)

## Problem

When a session misbehaves at turn 37, there is no record of what rule/check
configuration was active when the earlier turns ran. Check specs can be added
or edited mid-session (repo `.marionette/checks/` or `{state_dir}/checks/`),
and env toggles (hash edits, tool discovery, advisor) can differ between the
run that produced a regression and the run trying to reproduce it. v1 makes
the per-turn configuration *inspectable* after the fact; it does not replay
or re-enforce old configuration.

## Shape

- New module `harness/turn_context.py` (stdlib only).
- At each turn boundary (entry to `ConversationalSession.send`), append one
  JSON line to `{state_dir}/turn_context.jsonl`:

  ```json
  {"session_id": "s1", "turn": 3, "ts": 1751830000.0,
   "check_specs_hash": "sha256...", "check_spec_count": 2,
   "env": {"HARNESS_HASH_EDIT": "1", "HARNESS_ADVISOR": ""}}
  ```

  - `turn` is the 1-based count of user messages in history at send time.
  - `check_specs_hash` is a sha256 over the JSON-serialized, sorted check
    specs from `find_check_specs(repo, state_dir)`; empty string when no
    specs. This detects "the checks changed between turn 3 and turn 30"
    without storing full spec bodies.
  - `env` records the toggles that alter pilot behavior:
    `HARNESS_DECLARATIVE_CHECKS`, `HARNESS_HASH_EDIT`,
    `HARNESS_TOOL_DISCOVERY`, `HARNESS_ADVISOR`, `HARNESS_AST_PREVIEW`.
- Reader `context_at(state_dir, session_id, turn)` returns the newest record
  for that turn, or None.
- Endpoint `GET /api/session/context_at?turn=N` returns the record or 404.

## Integration map

| Piece | Location |
| --- | --- |
| Journal module | `harness/turn_context.py` (new) |
| Record hook | `harness/conversation.py` `send()` entry, wrapped try/except |
| API | `harness/server.py` GET handler next to `/api/session/state` |
| Tests | `tests/test_turn_context.py` (new) |

## Kill switch

`HARNESS_TURN_CONTEXT=0` disables recording (default on; recording is
append-only and read-only with respect to behavior).

## Non-goals (v1)

- Replaying old rules against new turns (enforcement).
- Storing full spec bodies or full env dumps.
- Migration of existing sessions (journal starts when the feature lands).

## Acceptance

- Record + read back across 3 turns; newest record wins per turn.
- Malformed journal lines are skipped; missing file yields None/404.
- Recording failure never breaks `send()`.
