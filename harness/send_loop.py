from __future__ import annotations

"""Send-loop mixin for ConversationalSession.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / BusyControlMixin
contract: these methods operate through `self` (history, busy lock, cancel,
display transcript, pilot, …) provided by the concrete class -- the mixin
defines no state and no __init__.

Owns the turn orchestration entrypoints ``send`` / ``_send_locked`` /
``_send_locked_inner`` plus the small private helpers that exist only to
support that loop (``_is_correction``, ``_get_codegraph_context``). Background
thread targets, stream-queue drain, per-step metering, prefetch pool,
idle steer/queue finalization, read-only/local tool-result assembly, auto-verify,
and action-goal labeling live in ``send_loop_phases``; the per-step action
spree (guards / prefetch / advisor / fan-out) lives in ``send_loop_actions``;
swarm/implement/parallel/route_task/memory dispatch lives in
``send_loop_dispatch`` so the kernel stays the public orchestration surface.
Busy lock lifecycle stays on BusyControlMixin; per-tool ``_do_*`` handlers
stay on ToolDispatchMixin.

CRITICAL invariants — zero behavior change:
- ConvEvent kinds/shapes unchanged
- busy acquire/release/generation unchanged
- SSE detach != cancel
- steer/queue/interrupt/resume semantics unchanged
- tool dispatch still calls mixin ``_do_*`` methods

Method Resolution Order keeps behavior identical: ``send`` still resolves via
inheritance on ConversationalSession.
"""

import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Iterator, Optional

from ._exec import _puppetmaster_available, _puppetmaster_cmd  # noqa: F401 — test patch surface
from .pilot import (
    PilotError,
    parse_pilot_turn,
)
from .send_loop_actions import execute_turn_actions
from .send_loop_phases import (
    drain_idle_turn,
    drain_stream_queue,
    meter_pilot_step,
    run_auto_verify,
    run_stream,
)
from .text_clean import clean_say
from .tool_dispatch import _strip_ansi, is_safe_path


class SendLoopMixin:
    """Mixin holding send-loop orchestration for ConversationalSession.

    The concrete class supplies the state these methods read/write via `self`.
    This mixin defines no __init__ and no instance state of its own.
    """

    def _is_correction(self, text: str) -> bool:
        t = text.lower()
        patterns = ["no,", "don't", "dont", "stop", "actually", "wrong", "not like that", "should be", "instead"]
        for p in patterns:
            if p in t:
                return True
        if getattr(self, "_total_tool_calls", 0) > 0:
            action_patterns = ["fix", "correct", "incorrect", "error", "failed", "bug", "mistake", "change"]
            for ap in action_patterns:
                if ap in t:
                    return True
        return False

    def send(self, user_message: str, images: Optional[list] = None, plan: bool = False, resume: bool = False) -> Iterator[ConvEvent]:
        """Process one user message: drive the pilot loop until it yields back.

        ``resume=True`` is the keep-alive continuation path: a background swarm
        finished and ``drain_swarm_results`` already appended the result record
        plus a user-role continuation to history. We generate off that existing
        history WITHOUT appending a new user turn, so the pilot autonomously
        assesses the result and takes the next step -- no new user message and no
        autopilot required.
        """
        from .conversation import ConvEvent
        # Keep-alive must not restart a turn the user just stopped. Real user /
        # autopilot sends clear the Stop hold in _mark_busy_acquired once they
        # own the lock.
        if resume and (
            getattr(self, "_stop_holds_idle", False)
            or getattr(self, "_interrupted_swarms", False)
        ):
            return
        self._cancel.clear()
        self._pending_advisor_warnings = []
        if not self._busy.acquire(blocking=False):
            # The lock is held. Normally that means a turn is genuinely streaming.
            # But if a previous turn's generator was never closed (hard crash /
            # abandoned stream), the lock LEAKS and the pilot looks dead forever.
            # Detect a stale lock -- held with no live stream for too long -- and
            # forcibly recover it so the user isn't permanently wedged.
            import time as _t
            held_for = _t.monotonic() - self._busy_since if self._busy_since else 0.0
            stale = self._busy_since and held_for > 1.5 and self._state == "idle"
            # If the user EXPLICITLY interrupted the previous turn, recover the
            # lock even when _state is still 'executing' (the abandoned turn is
            # blocked in a subprocess/tool and may never reach its finally). A
            # shorter grace here is safe because the user asked to stop -- this is
            # the "stop a chat right as it runs tool calls" case that wrongly
            # errored 'session busy'.
            if not stale and self._interrupt_requested and self._busy_since and held_for > 0.5:
                stale = True
            if stale:
                self._interrupt_requested = False
                # Advance the generation as we force-release so the leaked holder's
                # own finally (if it ever runs) treats its release as a no-op and
                # cannot free the lock this new turn is about to take.
                with self._busy_meta:
                    self._busy_gen += 1
                    self._busy_since = 0.0
                    try:
                        self._busy.release()
                    except RuntimeError:
                        pass
                if not self._busy.acquire(blocking=False):
                    yield ConvEvent("error", {"error": "session busy: another request is in flight"})
                    return
            else:
                yield ConvEvent("error", {"error": "session busy: another request is in flight"})
                return
        busy_gen = self._mark_busy_acquired()
        # Stream any Stop↔steer drop notice recorded by interrupt or the
        # post-Stop late-steer cleanup in _mark_busy_acquired.
        flush = getattr(self, "_flush_steer_drop_notice", None)
        if callable(flush):
            yield from flush()
        # Time-travel journal (round 6): snapshot the active check specs and
        # behavior toggles for this turn. Observability only; never raises.
        try:
            from .turn_context import record_turn_context
            from .memory_layers import (
                record_memory_layer_snapshot,
                snapshot_memory_layers,
            )

            _turn_index = sum(
                1 for m in self._history if m.get("role") == "user"
            ) + (0 if resume else 1)
            record_turn_context(
                self.state_dir,
                self.harness_session_id or "default",
                _turn_index,
                repo=self.config.repo or "",
            )
            record_memory_layer_snapshot(
                self.state_dir,
                self.harness_session_id or "default",
                _turn_index,
                snapshot_memory_layers(
                    self,
                    self.state_dir,
                    self.harness_session_id or "default",
                    repo=self.config.repo or "",
                ),
            )
        except Exception:
            pass
        if not resume and self._is_correction(user_message):
            self._corrections.append(user_message)
        original_sys = self._history[0]["content"]
        # Plan mode must NOT mutate the system prefix (busts prompt cache for
        # every provider under append-only). PLAN_SYSTEM_SUFFIX rides on the
        # user turn in _send_locked_inner instead; action filtering still uses
        # the plan= flag.
        try:
            import time
            action_starts = {}
            pending_cards = {}
            for ev in self._send_locked(user_message, images=images, plan=plan, resume=resume):
                if ev.kind == "action_start":
                    self._total_tool_calls += 1
                    aid = ev.data.get("id")
                    if aid:
                        action_starts[aid] = time.time()
                        goals = ev.data.get("goals")
                        if not isinstance(goals, list):
                            goals = None
                        card = {
                            "type": "card",
                            "id": aid,
                            "kind": ev.data.get("kind"),
                            "goal": ev.data.get("goal"),
                            "cwd": ev.data.get("cwd"),
                            # None = still running. Append immediately so session
                            # transcript polls / reattach see the tool row instead
                            # of wiping the live Investigating UI mid-command.
                            "result": None,
                        }
                        if goals is not None:
                            card["goals"] = [str(g) for g in goals if str(g or "").strip()]
                        call_id = str(ev.data.get("call_id") or "").strip()
                        if call_id:
                            card["call_id"] = call_id
                        elif not str(aid).startswith("a") or (len(str(aid)) > 1 and not str(aid)[1:].isdigit()):
                            # Stable provider ids double as call_id for prep promotion.
                            card["call_id"] = str(aid)
                        pending_cards[aid] = card
                        self._display_transcript.append(card)
                elif ev.kind == "action_result":
                    aid = ev.data.get("id")
                    if aid and aid in action_starts:
                        duration_ms = int((time.time() - action_starts[aid]) * 1000)
                        ev.data["duration_ms"] = duration_ms
                    # Enrich sparse results so ring-miss / missing-start clients
                    # can still create a durable card with kind/goal/status.
                    if aid and aid in pending_cards:
                        prior = pending_cards[aid]
                        if not ev.data.get("kind") and prior.get("kind"):
                            ev.data["kind"] = prior.get("kind")
                        if not ev.data.get("goal") and prior.get("goal"):
                            ev.data["goal"] = prior.get("goal")
                        if prior.get("goals") and not ev.data.get("goals"):
                            ev.data["goals"] = list(prior.get("goals") or [])
                        if prior.get("call_id") and not ev.data.get("call_id"):
                            ev.data["call_id"] = prior.get("call_id")
                        if prior.get("cwd") and not ev.data.get("cwd"):
                            ev.data["cwd"] = prior.get("cwd")
                    # Advisor warnings (round 6): surface once, on the first
                    # action_result after the advisor ran. Advisory only.
                    pending_warnings = getattr(self, "_pending_advisor_warnings", None)
                    if pending_warnings:
                        ev.data["advisor_warnings"] = list(pending_warnings)
                        self._pending_advisor_warnings = []
                    if ev.data.get("error"):
                        self._has_tool_failure = True
                    else:
                        if getattr(self, "_has_tool_failure", False):
                            self._error_then_recovery_seen = True

                    if aid and aid in pending_cards:
                        card = pending_cards[aid]
                        res_data = {}
                        for key in [
                            "job_id", "num", "types", "adapter", "artifacts",
                            "error", "duration_ms", "chars", "status", "message",
                        ]:
                            if key in ev.data:
                                res_data[key] = ev.data[key]
                        # In-place update of the action_start row (already in display).
                        card["result"] = res_data
                        del pending_cards[aid]
                    elif aid:
                        # Result without a tracked start -- still persist a card.
                        res_data = {}
                        for key in [
                            "job_id", "num", "types", "adapter", "artifacts",
                            "error", "duration_ms", "chars", "status", "message",
                        ]:
                            if key in ev.data:
                                res_data[key] = ev.data[key]
                        card = {
                            "type": "card",
                            "id": aid,
                            "kind": ev.data.get("kind"),
                            "goal": ev.data.get("goal"),
                            "cwd": ev.data.get("cwd"),
                            "result": res_data,
                        }
                        goals = ev.data.get("goals")
                        if isinstance(goals, list):
                            card["goals"] = [str(g) for g in goals if str(g or "").strip()]
                        call_id = str(ev.data.get("call_id") or "").strip()
                        if call_id:
                            card["call_id"] = call_id
                        self._display_transcript.append(card)

                if ev.kind == "assistant_done":
                    self._turn_count += 1
                    # Emit assistant_done first so the UI paints the final answer
                    # before any non-blocking Save/Skip cards.
                    yield ev
                    if self._auto_mode:
                        # Full-auto: never propose memory (no human to Save/Skip).
                        self._turn_memory_queue.clear()
                        # Full-auto mode: run synchronously to ensure sequential consistency
                        if self._auto_distill:
                            d = self._maybe_auto_distill()
                            if d:
                                yield ConvEvent("distilled", d)
                        if self._wiki_orchestrate:
                            try:
                                w = self.prepare_wiki_pages()
                                if w and w.get("status") == "prepared" and w.get("pages"):
                                    yield ConvEvent("wiki_prepared", w)
                            except Exception:
                                pass
                    else:
                        # Interactive: emit non-blocking memory Save/Skip cards
                        # AFTER the final answer (never mid-tool-loop).
                        for prop in self._flush_turn_memory_proposals():
                            yield ConvEvent("memory_propose", prop)
                        # Interactive mode: background the work to keep the UI
                        # responsive. Use housekeeping (not _submit_swarm) so
                        # distill/wiki never flip runners=running / Still working
                        # after assistant_done.
                        if self._auto_distill or self._wiki_orchestrate:
                            if not self._submit_housekeeping(
                                self._run_distill_and_wiki_background, user_message
                            ):
                                yield ConvEvent("notice", {
                                    "message": (
                                        "Could not start background distill/wiki "
                                        "this turn (best-effort)."
                                    )
                                })
                else:
                    yield ev
        finally:
            # Append-only freezes an enriched system prompt (MCP catalog, pilot
            # identity, …). Restoring the pre-turn base would desync history
            # from the frozen prefix and break prompt.startswith stability.
            if self._resolve_append_only() and self._frozen_system_prompt is not None:
                self._history[0]["content"] = self._frozen_system_prompt
            else:
                self._history[0]["content"] = original_sys
            self._release_busy(busy_gen)

    def _send_locked(self, user_message: str, images: Optional[list] = None, plan: bool = False, resume: bool = False) -> Iterator[ConvEvent]:
        from .conversation import ConvEvent
        self._state = "thinking"
        try:
            yield from self._send_locked_inner(user_message, images=images, plan=plan, resume=resume)
        finally:
            self._state = "idle"

    def _get_codegraph_context(self, query: str) -> str:
        """Build a relevance-ranked CodeGraph context block for ``query``.

        Shells out to ``python -m puppetmaster codegraph search <query>`` (same
        interpreter, cwd = the open repo), parses ``path:line`` hit locations,
        reads a small +/-8 line source window for the top hits, and returns a
        single <codegraph-context> ... </codegraph-context> block. Returns "" on
        any failure or when there are no hits. Fully exception-guarded: this
        NEVER raises into the pilot loop and degrades to a pure no-op.
        """
        MAX_HITS = 5
        WINDOW = 8
        MAX_BYTES = 4096
        repo = getattr(self.config, "repo", None)
        if not repo or not query or not query.strip():
            return ""
        from harness.context_budget import truncate_bytes
        try:
            cmd = [sys.executable, "-m", "puppetmaster", "codegraph", "search", query]
            p = subprocess.run(
                cmd,
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
            if p.returncode != 0:
                return ""
            output = _strip_ansi((p.stdout or ""))
        except Exception:
            return ""

        # Parse "path:line" hit locations (first two colon-separated fields where
        # the second is an integer line number). Dedupe, preserve rank order.
        hit_re = re.compile(r"([^\s:]+):(\d+)")
        seen: set = set()
        hits: list[tuple[str, int]] = []
        for line in output.splitlines():
            m = hit_re.search(line)
            if not m:
                continue
            path, lineno = m.group(1), int(m.group(2))
            key = (path, lineno)
            if key in seen:
                continue
            seen.add(key)
            hits.append((path, lineno))
            if len(hits) >= MAX_HITS:
                break
        if not hits:
            return ""

        blocks: list[str] = []
        for path, lineno in hits:
            try:
                abs_path = path if os.path.isabs(path) else os.path.join(repo, path)
                if not is_safe_path(abs_path, repo):
                    continue
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except Exception:
                continue
            start = max(0, lineno - 1 - WINDOW)
            end = min(len(lines), lineno + WINDOW)
            snippet = "".join(lines[start:end]).rstrip("\n")
            blocks.append(f"# {path}:{lineno}\n{snippet}")

        if not blocks:
            return ""
        body = "\n\n".join(blocks)
        body = truncate_bytes(body, MAX_BYTES)
        return f"<codegraph-context>\n{body}\n</codegraph-context>"

    def _send_locked_inner(self, user_message: str, images: Optional[list] = None, plan: bool = False, resume: bool = False) -> Iterator[ConvEvent]:
        from .conversation import (
            ConvEvent,
            _format_mcp_tools_section,
            _hard_pilot_steps,
            _prewarm_worker_imports,
        )
        if resume:
            # Keep-alive continuation: drain_swarm_results already appended the
            # result record + a user-role continuation. Generate off that history
            # WITHOUT appending anything. If the last turn is not a user message
            # there is nothing to respond to -- bail cleanly so a stray resume
            # trigger never fabricates an empty turn.
            if not (self._history and self._history[-1].get("role") == "user"):
                return
        else:
            processed_message = user_message
            if images:
                from .vision import transcribe_images
                yield ConvEvent("vision", {"count": len(images), "status": "transcribing"})
                results = transcribe_images(images)
                blocks = []
                for path, r in zip(images, results):
                    if r.error:
                        yield ConvEvent("vision", {"path": path, "error": r.error})
                    else:
                        blocks.append(f"[Image: {path}]\n{r.text}")
                        yield ConvEvent("vision", {"path": path,
                            "chars": len(r.text), "model": r.model,
                            "preview": r.text[:200]})
                if blocks:
                    processed_message = ("The user attached image(s). Transcription(s) below "
                                         "(you cannot see the image, only this text):\n\n"
                                         + "\n\n".join(blocks) + "\n\n---\n" + user_message)

            self._turn_output_tokens = 0
            self._turn_budget = None
            # Fresh user message: clear prior-step guard / stagnation / failed-
            # objective resume state so caps do not leak across unrelated turns.
            # Keep-alive resume leaves these intact for the originating turn.
            self._turn_guard_state = None
            self._stagnation_last_prose = None
            self._stagnation_last_actions = None
            self._stagnation_streak = 0
            self._failed_objective_resume_counts = {}
            try:
                from .turn_budget import turn_budget_enabled

                if turn_budget_enabled():
                    self._turn_budget = self._turn_economy.parse_output_directive(
                        user_message
                    )
            except Exception:
                pass

            if self._resolve_append_only():
                processed_message = self._append_turn_context_trailer(
                    processed_message, user_message
                )

            if plan:
                from .pilot import PLAN_SYSTEM_SUFFIX
                processed_message = (
                    processed_message.rstrip() + "\n\n" + PLAN_SYSTEM_SUFFIX
                )

            # Preserve strict user/assistant alternation in _history: if the last
            # message is already a user turn (e.g. a background job just drained a
            # pilot-resume continuation before the user typed), merge into it rather
            # than appending a second adjacent user message, which some chat APIs
            # (Anthropic) reject and the concurrency stress test forbids.
            if self._history and self._history[-1].get("role") == "user":
                self._history[-1]["content"] = (
                    self._history[-1]["content"].rstrip() + "\n\n" + processed_message
                )
            else:
                self._history.append({"role": "user", "content": processed_message})
            self._display_transcript.append({"type": "message", "role": "user", "text": user_message})

            # Inject relevance-ranked CodeGraph context (best-effort, exception-guarded)
            # so the driver sees the most relevant code BEFORE it starts calling tools.
            # Skip for no_delegation worker sessions (they run in a fresh worktree with
            # no CodeGraph index). Degrades to a no-op when codegraph is unavailable.
            if (
                not getattr(self.config, "no_delegation", False)
                and not self._resolve_append_only()
            ):
                cg_context = self._get_codegraph_context(user_message)
                if cg_context:
                    self._history.append({"role": "user", "content": cg_context})

        swarms = 0
        action_seq = 0
        demo_swarms = 0  # count swarms that returned the demo substrate
        turn_findings: list = []   # accumulate real findings for wiki ingest
        turn_prose: list = []      # accumulate pilot prose for the digest

        consecutive_non_productive = 0
        # AUTO-VERIFY LOOP: after a turn that edited files, run a fast, scoped
        # project check and feed a FAILURE back as a tool observation IN THE SAME
        # user message so the pilot can self-correct. Bounded per user message so
        # it cannot loop forever.
        auto_verify_iters = 0
        try:
            _auto_verify_cap = int(os.environ.get("HARNESS_AUTO_VERIFY_MAX", "2"))
        except ValueError:
            _auto_verify_cap = 2
        # Step ceiling per user message, read LIVE from the env each turn so a
        # Settings change applies without a restart. 0 (or negative) means
        # UNLIMITED -- true autopilot: loop until the pilot is done, the budget
        # governor halts it, or the user stops it. Otherwise cap at 2x the
        # configured pilot-step budget.
        import itertools as _itertools
        _hard_steps = _hard_pilot_steps()
        try:
            _pilot_steps = int(os.environ.get("HARNESS_MAX_PILOT_STEPS", str(_hard_steps)))
        except ValueError:
            _pilot_steps = _hard_steps
        if _pilot_steps <= 0:
            _step_iter = _itertools.count()
            max_steps = 0  # 0 == unlimited (used by the limit message below)
        else:
            max_steps = 2 * _pilot_steps
            _step_iter = range(max_steps)

        # Advisory compaction once per user turn (after the new user message is
        # in history), NOT at the start of every tool-loop step. Mid-turn
        # history rewrites bust prefix cache for all providers. CONTEXT_OVERFLOW
        # still force-compacts inside the step loop as a last resort.
        yield from self._maybe_compact_history()

        for step in _step_iter:
            # Heal dangling tool pairs from a prior mid-spree abandon (steer/
            # cancel) BEFORE the cancel check so an interrupt never freezes
            # invalid history into the next request / export.
            self._sanitize_tool_pairs()
            if self._cancel.is_set():
                flush = getattr(self, "_flush_steer_drop_notice", None)
                if callable(flush):
                    yield from flush()
                yield ConvEvent("interrupted", {"reason": "session interrupted"})
                return

            # Consume any pending steer at the start of the step: it's now in
            # history and the model will see it this iteration, so clear the flag.
            # (_check_and_inject_steer itself refuses inject after Stop.)
            self._steer_pending = False
            yield from self._check_and_inject_steer()
            self._steer_pending = False

            # 1. Ask the pilot for its next conversational turn.
            base_sys = self._history[0]["content"]
            cg_section = ""
            # Skip the per-turn CodeGraph context build for no_delegation worker sessions:
            # a worker runs in a fresh git worktree with NO .codegraph index, so this call
            # blocks on a 30s timeout EVERY turn and returns nothing -- it was ~93% of worker
            # wall-time. Workers edit directly and do not use codegraph (it is also excluded
            # from their toolset), so skipping it is pure win.
            _no_deleg = getattr(self.config, "no_delegation", False)
            cg_symbol_count = 0
            append_only = self._resolve_append_only()
            if self.config.repo and not _no_deleg and not append_only:
                # Cache the CodeGraph slice per user message: the underlying
                # codegraph_context() is a blocking Node subprocess (~270-500ms).
                # Recomputing it on every step of a multi-step turn (identical
                # query) just stacks dead time in front of the model. Compute it
                # once on the first step, reuse it for the rest of this turn.
                if self._cg_cache_key == user_message:
                    cg_section = self._cg_cache_section
                    cg_symbol_count = self._cg_cache_symbols
                else:
                    try:
                        from puppetmaster.codegraph import codegraph_context, codegraph_prompt_section
                        cg_slice = codegraph_context(task=user_message, cwd=self.config.repo)
                        if cg_slice:
                            # Count located symbols (entry points + related symbols) so the
                            # UI can show that CodeGraph was consulted this turn.
                            cg_symbol_count = cg_slice.count("- **") + cg_slice.count("#### ")
                            # Prepend an AUTHORITATIVE directive so the model leans on the
                            # already-injected CodeGraph slice instead of redundantly raw-reading
                            # whole files (qwen tends to dump files even with context present).
                            authoritative = (
                                "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK. The relevant "
                                "symbols, definitions, and code are provided in the section below. "
                                "USE THIS as your primary source. Do NOT re-read entire files that "
                                "already appear here -- only read_file specific additional lines you "
                                "still need (with start_line + limit), or call search_codegraph to "
                                "widen the graph. Whole-file dumps when the answer is already below "
                                "are wasteful and wrong.\n"
                            )
                            cg_section = authoritative + codegraph_prompt_section(cg_slice)
                        # Cache the result (even an empty slice) so we never re-run
                        # the subprocess for the same message this turn.
                        self._cg_cache_key = user_message
                        self._cg_cache_section = cg_section
                        self._cg_cache_symbols = cg_symbol_count
                        # Visibility: tell the UI CodeGraph was consulted -- only on
                        # the first compute, so the chip shows once per turn.
                        if cg_section and not _no_deleg:
                            yield ConvEvent("codegraph_context", {
                                "symbols": cg_symbol_count,
                                "query": (user_message or "")[:120],
                            })
                    except Exception:
                        pass

            wiki_section = ""
            if self._wiki.configured and not append_only:
                if self._wiki_cache_key == user_message:
                    wiki_section = self._wiki_cache_section
                else:
                    wiki_section = self._build_turn_wiki_section(user_message)

            resp = None
            self._streamed_prose = ""  # reset per step; set if this step streams
            for attempt in range(2):
                # Sanitize BEFORE rendering/dispatch so both chat() and
                # complete() see healed tool_use/tool_result pairs. (A prior
                # interrupted spree — cancel/steer/worker-ceiling/exception —
                # otherwise 400s the next provider request.)
                self._sanitize_tool_pairs()
                if append_only:
                    sys_prompt = self._ensure_frozen_system_prompt(base_sys)
                    prompt = self._render_history()
                    self._record_prompt_stability(prompt)
                else:
                    sys_prompt = base_sys
                    if cg_section:
                        sys_prompt += "\n\n" + cg_section
                    if wiki_section:
                        sys_prompt += "\n\n" + wiki_section
                    mcp_section = _format_mcp_tools_section(
                        self._mcp,
                        self._tool_catalog,
                        no_delegation=getattr(self.config, "no_delegation", False),
                        browser_enabled=getattr(self.config, "browser_enabled", True),
                    )
                    if mcp_section:
                        sys_prompt += "\n\n" + mcp_section
                    turn_note = self._turn_budget_system_note()
                    if turn_note:
                        sys_prompt += "\n\n" + turn_note
                    identity_note = self._pilot_identity_system_note()
                    if identity_note:
                        sys_prompt += "\n\n" + identity_note
                    adapter_note = self._active_adapters_system_note()
                    if adapter_note:
                        sys_prompt += "\n\n" + adapter_note

                    self._history[0]["content"] = sys_prompt
                    prompt = self._render_history()

                try:
                    if hasattr(self.pilot, "chat"):
                        tools_schema = self._build_visible_tools_schema()

                        is_interactive = not getattr(self.config, "no_delegation", False)
                        # Gate on an EXPLICIT capability flag (is True) + a callable chat_stream.
                        # Using `is True` avoids MagicMock test pilots (which fabricate any attr as a
                        # truthy Mock) wrongly entering the streaming branch.
                        _can_stream = (
                            getattr(self.pilot, "supports_streaming", False) is True
                            and callable(getattr(self.pilot, "chat_stream", None))
                        )
                        if is_interactive and _can_stream:
                            import queue
                            import threading
                            q = queue.Queue()

                            t = threading.Thread(
                                target=run_stream,
                                args=(self, q, tools_schema, sys_prompt),
                                daemon=True,
                            )
                            t.start()

                            streamed_prose, resp = yield from drain_stream_queue(q)
                            self._streamed_prose = streamed_prose
                        else:
                            resp = self.pilot.chat(
                                self._messages_for_provider(),
                                tools=tools_schema,
                                system=sys_prompt,
                            )
                    else:
                        resp = self.pilot.complete(prompt, system=sys_prompt)
                except Exception as e:
                    yield ConvEvent("error", {"error": f"pilot transport: {e}"})
                    return
                finally:
                    if not append_only:
                        self._history[0]["content"] = base_sys

                meter_pilot_step(self, resp, prompt)

                if resp and resp.error:
                    from pmharness.drivers import error_classifier
                    err_cls = error_classifier.classify(None, resp.error)
                    if err_cls == error_classifier.ErrorClass.CONTEXT_OVERFLOW:
                        if attempt == 0:
                            # Force history compaction and try again
                            yield from self._maybe_compact_history(
                                force=True, emergency=True,
                            )
                            continue
                        else:
                            # Context overflow persists after compaction
                            yield ConvEvent("error", {"error": "context overflow persists after compaction"})
                            return

                # If there's no error or it is not context overflow, we're done
                break

            if resp and resp.error:
                yield ConvEvent("error", {"error": self._humanize_pilot_error(resp.error)})
                return

            is_native = False
            tool_calls = []
            reasoning = ""
            pure_content = ""

            if hasattr(self.pilot, "chat"):
                tool_calls = resp.meta.get("tool_calls") or []
                reasoning = resp.meta.get("reasoning") or ""
                pure_content = resp.text or ""

                if tool_calls or reasoning:
                    is_native = True
                elif pure_content:
                    from .pilot import _extract_json_object
                    obj = _extract_json_object(pure_content)
                    if obj and isinstance(obj, dict) and ("say" in obj or "actions" in obj or "thinking" in obj):
                        is_native = False
                    else:
                        is_native = True
                else:
                    is_native = True

            if is_native:
                try:
                    from .pilot import parse_tool_calls, PilotTurn, parse_inline_tool_calls, strip_inline_tool_calls
                    if not tool_calls and pure_content:
                        inline_actions = parse_inline_tool_calls(pure_content)
                        if inline_actions:
                            import json
                            synthetic_tool_calls = []
                            for act in inline_actions:
                                name = act.kind
                                if act.kind == "call_mcp" and act.tool:
                                    name = f"mcp_{act.tool.replace('.', '_')}"
                                synthetic_tool_calls.append({
                                    "id": act.tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(act.arguments)
                                    }
                                })
                            tool_calls = synthetic_tool_calls
                            actions = inline_actions
                            pure_content = strip_inline_tool_calls(pure_content)
                        else:
                            actions = parse_tool_calls(tool_calls)
                    else:
                        actions = parse_tool_calls(tool_calls)

                    turn = PilotTurn(say=pure_content, thinking=reasoning, actions=actions)
                except Exception as e:
                    yield ConvEvent("error", {"error": f"native tool parsing error: {e}"})
                    return
            else:
                try:
                    turn = parse_pilot_turn(resp.text)
                except PilotError as e:
                    # one lenient retry: tell the pilot to fix its envelope
                    self._history.append({"role": "user",
                        "content": f"(system) Your last reply was not valid. {e}. "
                                   f"Reply with the JSON envelope {{\"say\":...,\"actions\":[...]}}."})
                    continue

            # 2. Emit the pilot's prose to the user.
            # Do not emit a "thinking"/reasoning ConvEvent. Streaming already
            # paints the answer first; a late reasoning block after the answer
            # is redundant UI and (when enable_reasoning is on) wasted tokens.
            # Pilot JSON "thinking" fields are still parsed into turn.thinking
            # for internal use, but never shown.

            cleaned_say_text = clean_say(turn.say) if turn.say else ""
            if cleaned_say_text:
                # If this prose was already streamed token-by-token, flag it so the
                # frontend finalizes the existing streaming bubble in place instead
                # of treating it as a brand-new message (which would re-dump it).
                _already_streamed = bool(self._streamed_prose.strip())
                yield ConvEvent("message", {"role": "assistant", "text": cleaned_say_text, "streamed": _already_streamed})
                turn_prose.append(cleaned_say_text)
                self._display_transcript.append({"type": "message", "role": "assistant", "text": cleaned_say_text})
            # record the pilot's turn in transcript (prose only -- the conversation)
            if is_native:
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if cleaned_say_text:
                    assistant_msg["content"] = cleaned_say_text
                else:
                    assistant_msg["content"] = ""
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                self._history.append(assistant_msg)
            else:
                self._history.append({"role": "assistant", "content": cleaned_say_text or "(acting)"})

            if self._turn_budget_exhausted():
                # Close the turn for the UI before wiki ingest (network I/O).
                yield ConvEvent("assistant_done", {
                    "turns": step + 1,
                    "swarms": swarms,
                    "turn_budget_exhausted": True,
                })
                self._submit_housekeeping(
                    self._maybe_ingest,
                    user_message, list(turn_prose), list(turn_findings),
                )
                return

            if len(turn.actions) > 0 or (cleaned_say_text and len(cleaned_say_text.strip()) > 0):
                consecutive_non_productive = 0
            else:
                consecutive_non_productive += 1

            if consecutive_non_productive >= 3:
                break

            # Stagnation governor: repeated normalized assistant prose plus the
            # same action fingerprint with no new progress ends the turn calmly
            # (including when HARNESS_MAX_PILOT_STEPS=0). Distinct actions or
            # real progress reset the streak.
            try:
                from .pilot_guards import (
                    fingerprint_turn_actions,
                    normalize_assistant_prose,
                    stagnation_streak_cap,
                )
                prose_key = normalize_assistant_prose(cleaned_say_text)
                action_key = fingerprint_turn_actions(turn.actions)
                # Empty turns (no prose, no actions) are handled by the
                # consecutive_non_productive break above — skip fingerprinting.
                if prose_key or action_key:
                    prev_prose = getattr(self, "_stagnation_last_prose", None)
                    prev_actions = getattr(self, "_stagnation_last_actions", None)
                    if (
                        prev_prose is not None
                        and prev_actions is not None
                        and prose_key == prev_prose
                        and action_key == prev_actions
                    ):
                        self._stagnation_streak = int(
                            getattr(self, "_stagnation_streak", 0) or 0
                        ) + 1
                    else:
                        # First sighting of this fingerprint starts the streak.
                        self._stagnation_streak = 1
                    self._stagnation_last_prose = prose_key
                    self._stagnation_last_actions = action_key
                    if self._stagnation_streak >= stagnation_streak_cap():
                        halt_msg = (
                            "Stopped: repeated the same response and actions "
                            "with no new progress (auto-halt). Tell me how to "
                            "continue, or try a narrower ask."
                        )
                        # Heal any tool_call pairing from this assistant turn
                        # before exiting so history stays valid for the next send.
                        self._sanitize_tool_pairs()
                        yield ConvEvent("notice", {
                            "message": halt_msg,
                            "kind": "stagnation",
                        })
                        yield ConvEvent("message", {
                            "role": "assistant",
                            "text": halt_msg,
                        })
                        self._display_transcript.append({
                            "type": "message",
                            "role": "assistant",
                            "text": halt_msg,
                        })
                        yield ConvEvent("assistant_done", {
                            "turns": step + 1,
                            "swarms": swarms,
                            "stagnation_halt": True,
                        })
                        self._submit_housekeeping(
                            self._maybe_ingest,
                            user_message, list(turn_prose), list(turn_findings),
                        )
                        return
            except Exception:
                pass

            # 3. No actions => the pilot is done talking. Before yielding back to
            # the user, drain any pending steer. A steer that arrives while the
            # model is finalizing has no tool result to piggyback on (the last
            # history message is this assistant turn), so the mid-spree path in
            # _check_and_inject_steer cannot deliver it. We deliver it here as a
            # genuine next-turn user message (valid assistant -> user alternation)
            # and re-ask the model instead of terminating. This is the second of
            # the two steer delivery points (the other being the mid-spree
            # piggyback inside _check_and_inject_steer); together they guarantee
            # any enqueued steer is eventually delivered and never stranded.
            if not turn.has_actions:
                disposition, user_message = yield from drain_idle_turn(
                    self,
                    user_message=user_message,
                    step=step,
                    swarms=swarms,
                    turn_prose=turn_prose,
                    turn_findings=turn_findings,
                )
                if disposition == "continue":
                    continue
                if disposition == "break":
                    break
                return

            # 4. Execute each action as a collapsible tool-call.
            _action_counters = {
                "action_seq": action_seq,
                "swarms": swarms,
                "demo_swarms": demo_swarms,
            }
            _action_disposition, turn_changed_files = yield from execute_turn_actions(
                self,
                turn=turn,
                user_message=user_message,
                is_native=is_native,
                plan=plan,
                counters=_action_counters,
                step=step,
                turn_findings=turn_findings,
            )
            action_seq = _action_counters["action_seq"]
            swarms = _action_counters["swarms"]
            demo_swarms = _action_counters["demo_swarms"]
            if _action_disposition == "return":
                # Cancel mid-spree: heal unanswered tool_calls before exit so
                # the next send/resume/export never sees a dangling tool_use.
                self._sanitize_tool_pairs()
                return

            # ---- AUTO-VERIFY LOOP ----------------------------------------
            # After this batch of actions, IF the pilot edited any files AND
            # auto-verify is enabled, run a FAST, scoped project check and, on
            # FAILURE, inject the output as a tool observation into history and
            # re-ask the model IN THE SAME user message so it self-corrects
            # without the user pointing out the mistake. Bounded by
            # _auto_verify_cap so it cannot loop forever. Silent on pass.
            auto_verify_iters, _verify_again = yield from run_auto_verify(
                self,
                turn_changed_files=turn_changed_files,
                auto_verify_iters=auto_verify_iters,
                auto_verify_cap=_auto_verify_cap,
                plan=plan,
            )
            if _verify_again:
                continue

        # Hit the step cap -- close the turn gracefully.
        limit_msg = "(Reached the investigation step limit for this message.)"
        yield ConvEvent("message", {"role": "assistant", "text": limit_msg})
        self._display_transcript.append({"type": "message", "role": "assistant", "text": limit_msg})
        yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms})
        self._submit_housekeeping(
            self._maybe_ingest,
            user_message, list(turn_prose), list(turn_findings),
        )

