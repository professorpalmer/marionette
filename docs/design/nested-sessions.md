# Nested sessions

A pilot can start a child session for one part of its plan. The child is a
real session: the user can open it, talk to it, steer it, and stop it. When
the child ends a turn that the parent started, the parent gets a short report
and continues on its own.

## Caller usage first

The pilot calls one tool:

```json
{"name": "nested_session", "arguments": {
  "action": "start", "name": "vehicle-return",
  "brief": "Map story warps against the vehicle return place. ...",
  "todo": "Map story warps against the vehicle return place"
}}
```

- `start` makes the child, its git worktree and branch, and runs its first turn.
- `message` sends text to a child. A busy child gets a steer. An idle child
  gets a new turn.
- `stop` stops the child turn.
- `list` shows the children of this session with status and last report.

The user sees:

- The rail shows the children under the parent, with a tree line. A child with
  a running turn shows a status dot. A collapsed parent shows "N nested sessions".
- The parent TODO list shows a chip on a step that a child owns:
  `vehicle-return · Done`. A click opens the child.
- The parent transcript shows one note row for each report.

## Sketch A: a child is a Marionette session (chosen)

A child is a normal `SessionStore` row plus a `nest` link. Its runner is a
`ConversationalSession` in the same runner registry as every other session.
The backend drives a child turn with no browser attached: a headless SSE
handler runs the normal `stream_chat` path, so the ring buffer, transcript
checkpoints, title, hooks, and `finalize_turn` stay the same. The UI opens a
child with the normal session switch, and a running turn reattaches through
the event ring.

## Sketch B: a child is a Puppetmaster worker job shown as a session

A child is a `puppetmaster_start_implement` job. The rail shows the job as a
session row. Steer goes through `puppetmaster_steer`. A follow-up message is
a new job with `resume_from`. A flow `wait` wakes the parent.

## Comparison

| Need | A: Marionette session | B: PM worker as a session view |
| --- | --- | --- |
| Restart | Row, transcript, link, and inbox persist. A turn cut by a restart is marked `stopped`, and the boot pass sends the child one resume turn. | The worker process survives a restart. |
| Steer and talk | Same composer, steer queue, and Stop as every session. | Steer only. Each user message is a new resumed job. No Marionette tools in the child. |
| Model | Own pilot model per session (picker). | The worker adapter model, not the Marionette pilot. |
| Cost | Session meters per row, same as every session. | PM receipts. Already shown in the Swarm Tracker. |
| Worktree | `git worktree add` per child, locked so cleanup keeps it. | PM implement worktree, discarded or merged at job end. |
| Switch speed | Normal session switch: warm cache, ring reattach. | A worker transcript must be converted for the chat view on each open. |
| New code | One module, one tool, a headless driver, rail and chip UI. | A job-to-transcript view, a composer that maps to steer and resume, report mapping. |

B wins only on process survival across a restart. A wins on every user-facing
need: "a real session" means the same composer, model, and tools as every
other session. A child can still fan work out to Puppetmaster workers from
inside its own turns. A needs no Puppetmaster change.

## Data model

Child row (`sessions.json`):

```json
{"id": "c1", "title": "vehicle-return", "workspace_root": "<parent project root>",
 "repo": "<worktree path>", "branch": "nest/vehicle-return-c1",
 "nest": {"parent_id": "p1", "name": "vehicle-return", "todo": "Map story warps ...",
          "worktree": "<worktree path>", "branch": "nest/vehicle-return-c1",
          "status": "working", "report": "", "updated": 1763130000.0,
          "driver": "parent"}}
```

- `workspace_root` stays the parent project, so the child lists in the same
  project. The runner works in `nest.worktree`.
- `status`: `working` (a turn runs), `done` (the turn ended), `failed` (the
  turn ended with an error), `stopped` (Stop, or a restart cut the turn).
- `driver`: who started the current turn, `parent` or `user`.

Parent row: `nest_inbox` holds notes not yet put in the parent history:

```json
{"nest_inbox": [{"child_id": "c1", "name": "vehicle-return", "status": "done",
                 "report": "first 400 chars of the final reply", "wake": true, "at": 1763130000.0}]}
```

Plan-step link: a TODO item gets `session` (child id) and `session_name`.

## Report-back rule

1. `finalize_turn` of a child turn calls `on_turn_end`. It sets `status` and
   `report`, and puts one note in the parent inbox. `wake` is true only for a
   turn that the parent started and that did not stop or fail.
2. Delivery: when the parent is idle, each note goes into the parent history
   as a user message and into the display as a `session_note` row.
3. Wake: if a delivered note has `wake`, no sibling child is `working`, and
   the parent made fewer than 8 automatic turns since the last user message,
   the backend runs one headless resume turn on the parent.
4. A busy parent keeps the notes. Its own `finalize_turn` delivers them, and
   a retry thread (one for each parent, every 0.5 s for 1 min) covers a note
   that lands while that turn closes.

## Robustness rules

- **Status is live.** Only a turn that runs now is `working`: a running
  runner, or a turn that `_begin` started and that has not ended (its token
  is in `_in_flight`). A stored `working` with neither shows `stopped`. The
  session list (`with_live_status`) and the chips use the same rule.
- **Every turn ends once.** `_begin` gives each turn a token. The headless
  thread calls `turn_exited(sid, token, outcome)` when it ends. If
  `finalize_turn` did not end the turn, `turn_exited` ends it, so the status
  leaves `working` and the parent gets a `failed` report. A late exit of an
  old turn does not touch the next turn.
- **A report arrives once.** Each note has an `id`. Delivery reads the inbox,
  adds only notes whose `session_note` row is not in the parent transcript,
  saves the parent, and only then drops the notes from the inbox. A failed
  save or a crash before the drop keeps the notes, and the next delivery
  does not add them again.
- **Children never share files.** Each child gets its own locked git
  worktree and branch. If the worktree cannot be made, the child is not
  made. A child never falls back to the project folder.
- **Restart.** Boot marks each child that was `working` as `stopped` and
  sends it one "continue" turn (`HARNESS_NESTED_RESUME_ON_BOOT=0` turns this
  off). Waiting inboxes are delivered.

## Load rules

- No poll for each child. The rail tree and the chips read the one shared
  `/api/session/state` poll (`nest.rev`, `nest.children`). That poll runs
  each 1 s while a child of the shown session works, else each 4 s.
- The rail reloads its rows once when `nest.rev` moves, not on each poll.
- A child opens through the normal `switchSession` path. There is no second
  switch path.
- A child worktree starts from a clone of the project CodeGraph index
  (`seed_codegraph`). Without it, the first read in the child ran a full
  `codegraph init --index` of the worktree.

## Comparison with the reference (Claude desktop nested sessions)

| Reference behavior | Marionette | Gap |
| --- | --- | --- |
| Children sit under the parent with a tree line | Yes | None |
| A status dot shows while a child works | Yes. The dot comes from the live status. | None |
| A collapsed parent shows "N nested sessions" | Yes. The open state is kept for each parent. | None |
| Plan step chip "name · Status" opens the child | Yes | Chip status follows the shared poll (1 s while a child works), not a push. |
| A child is a full session: transcript, model, worktree | Yes | No `model` choice for each child. A child takes the parent's pilot. |
| The user opens, talks to, steers, and stops a child | Yes, live proof | None |
| The header names the project and the child branch | Yes (fixed: it showed "Marionette" and the worktree folder) | None |
| A child turn end sends a short report to the parent | Yes, 400 characters, once | None |
| The parent continues when no child works | Yes, one wake turn | None |
| The parent messages a child | Yes. A busy child gets a steer. | A message to a child that is starting is refused. The parent sends it again. |
| A cap on how many run | Runner lease cap (default 3) | Over the cap the child fails with a report. It is not queued. |
| Deleting a parent | Children stay as top-level rows | Children and worktrees are not cleaned up with the parent. |
| Merge of child work | The parent merges the branch itself | Same as the reference: the parent asks the user. |
| The rail Branches list while a child is open | It marks the project branch (`main`) as current | The child branch is not marked. The header shows it. |

## Switch cost (measured 2026-11-14)

The switch rig of the session-switch work (production build, CDP mouse
click after a 150 ms hover, IPC timed in the Electron main process), 6 runs
each way on the same project:

| Switch | Transcript paint | All panels final | IPC calls | Long tasks |
| --- | --- | --- | --- | --- |
| Parent to parent | 12 ms | 11 ms | 21 | none |
| Parent to child | 11 ms | 11 ms | 22 | none |

A child switch changes each panel once, from the old final value to the new
one. No wrong value shows between them.

## Module map

- `harness/nested_sessions.py`: link and status types, report text, note
  text, wake rule, and `NestedSupervisor` (start, message, stop, list,
  `on_turn_end`, boot reconcile).
- `harness/headless_turn.py`: a null SSE handler and one function that runs
  `stream_chat` for a session on a daemon thread.
- `harness/server.py`: `ensure_background_runner` (runner with no view
  change) and the supervisor wiring in `finalize_turn` and boot.
- `harness/sessions.py`: `SessionStore` methods for `nest` and `nest_inbox`.
- `harness/todo.py`: `session` and `session_name` on `TodoItem`.
- UI: `webapp/src/lib/nestedSessions.ts` (tree and chip data),
  `webapp/src/components/NestedSessionRows.tsx` (rail tree),
  `ComposerTodoPanel.tsx` (chips), and the transcript `session_note` row.

## Red flags rejected

- A second turn engine for children. The headless driver reuses `stream_chat`.
- A parent-to-child pointer list. The children are found by `nest.parent_id`,
  so one fact has one owner.
- A wake on each child report. The parent wakes once, when no sibling works.
