# Session decisions — 2026-07-09 (v0.9.26)

## Restore points must be session- and repo-scoped

- Symptom: History showed / could restore another session's (or stale)
  checkpoints; risk of reverting a different project's tree after a
  session/project switch.
- Cause: Checkpoint metadata was repo_hash-keyed but entries had no
  `session_id`; list/restore ignored the active session; CheckpointsPane
  fetched once on mount and kept painting the previous list.
- Fix: stamp `session_id` on snapshot; filter list by active session;
  refuse restore/diff for mismatched stamped sessions and wrong
  `expected_repo`; History clears + refetches on repo/session scope change.
  Stamped rows stay hidden when no active session (legacy unstamped
  entries remain repo-bound only).

## Resizable Branches + prune stale edit branches

- Branches list was hard-capped at 140px; now drag-resizable like Session
  Jobs, height persisted in localStorage.
- `delete_branch` only deleted `pmworker-*` and no-op'd `pmedit-*` created
  by edit engines — throwaways leaked into Branches. Now both prefixes
  delete; `prune_orphan_edit_branches` + UI broom +
  `/api/worktrees/prune-edit-branches` remove orphans not attached to a
  worktree / current checkout.
