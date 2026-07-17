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
and action-goal labeling live in ``send_loop_phases`` so the kernel stays the
public orchestration surface. Busy lock lifecycle stays on BusyControlMixin;
per-tool ``_do_*`` handlers stay on ToolDispatchMixin.

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

from pmharness.intent import DriverIntent

from ._exec import _puppetmaster_available, _puppetmaster_cmd
from .diag import note as _diag_note
from .pilot import (
    PilotAction,
    PilotError,
    PilotTurn,
    is_invalid_action,
    parse_pilot_turn,
)
from .pilot_guards import (
    check_backend_restart,
    check_cli_redirect,
    check_pilot_guards,
    cli_redirect_enabled,
    dedupe_dispatch_actions,
    guards_active,
    new_turn_guard_state,
    record_action_execution,
)
from .send_loop_phases import (
    LOCAL_ACTION_KINDS,
    READ_ONLY_KINDS,
    action_display_goal,
    dispatch_local_action,
    dispatch_readonly_action,
    drain_idle_turn,
    drain_stream_queue,
    meter_pilot_step,
    read_stdout_thread,
    run_auto_verify,
    run_parallel_prefetch,
    run_stream,
    stream_swarm,
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
                        pending_cards[aid] = card
                        self._display_transcript.append(card)
                elif ev.kind == "action_result":
                    aid = ev.data.get("id")
                    if aid and aid in action_starts:
                        duration_ms = int((time.time() - action_starts[aid]) * 1000)
                        ev.data["duration_ms"] = duration_ms
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
                        for key in ["job_id", "num", "types", "adapter", "artifacts", "error", "duration_ms", "chars"]:
                            if key in ev.data:
                                res_data[key] = ev.data[key]
                        # In-place update of the action_start row (already in display).
                        card["result"] = res_data
                        del pending_cards[aid]
                    elif aid:
                        # Result without a tracked start -- still persist a card.
                        res_data = {}
                        for key in ["job_id", "num", "types", "adapter", "artifacts", "error", "duration_ms", "chars"]:
                            if key in ev.data:
                                res_data[key] = ev.data[key]
                        self._display_transcript.append({
                            "type": "card",
                            "id": aid,
                            "kind": ev.data.get("kind"),
                            "goal": ev.data.get("goal"),
                            "cwd": ev.data.get("cwd"),
                            "result": res_data,
                        })

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
            # Fresh user message: clear prior-step guard state so swarm-gate
            # redirect caps do not leak across unrelated turns.
            self._turn_guard_state = None
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
            if self._cancel.is_set():
                yield ConvEvent("interrupted", {"reason": "session interrupted"})
                return

            # Consume any pending steer at the start of the step: it's now in
            # history and the model will see it this iteration, so clear the flag.
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

                # Guarantee tool_use/tool_result pairing so a prior interrupted
                # spree (cancel/steer/worker-ceiling/exception) can't 400 the next
                # request with a dangling tool_use.
                self._sanitize_tool_pairs()
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
                            resp = self.pilot.chat(self._elide_stale_reads(self._history[1:]), tools=tools_schema, system=sys_prompt)
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
                            yield from self._maybe_compact_history(force=True)
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
            prior_guard = getattr(self, "_turn_guard_state", None)
            guard_state = new_turn_guard_state(user_message)
            # Carry swarm-gate redirect progress across model steps in this send()
            # so broad-intent turns cannot re-burn a full SUPPRESSED payload every
            # step before the model finally dispatches run_swarm.
            if prior_guard is not None:
                guard_state.swarm_gate_suppress_count = getattr(
                    prior_guard, "swarm_gate_suppress_count", 0
                )
            self._turn_guard_state = guard_state
            guard_suppressed: dict[int, Any] = {}
            guard_recorded_indices: set[int] = set()
            prefetch = {}
            read_actions_with_idx = []
            prefetch_targets = []
            for idx, act in enumerate(turn.actions):
                if act.kind in READ_ONLY_KINDS:
                    read_actions_with_idx.append((idx, act))
                    if guards_active():
                        guard_verdict = check_pilot_guards(guard_state, act.kind, act)
                        if guard_verdict.suppress:
                            if getattr(guard_verdict, "replay", False):
                                guard_suppressed[idx] = guard_verdict
                                # Replay still counts toward the loop-repeat cap.
                                try:
                                    record_action_execution(guard_state, act.kind, act)
                                except Exception:
                                    pass
                                continue
                            # Defer hard loop-suppress to execution time so an
                            # earlier identical action in this turn can populate
                            # the successful-result cache for replay. Other
                            # guards (swarm_gate, delegate, budget) still apply
                            # immediately.
                            if getattr(guard_verdict, "reason", "") == "loop":
                                continue
                            guard_suppressed[idx] = guard_verdict
                            continue
                        record_action_execution(guard_state, act.kind, act)
                        guard_recorded_indices.add(idx)
                    prefetch_targets.append((idx, act))

            if len(prefetch_targets) >= 2 and not self._cancel.is_set():
                prefetch = run_parallel_prefetch(self, prefetch_targets)

            # Advisor pass (round 6, opt-in): one read-only review of this
            # turn's pending action list. Warnings are attached to the first
            # action_result of the turn in send(); execution never blocks.
            try:
                from .advisor import advise, advisor_enabled

                if turn.actions and advisor_enabled():
                    self._pending_advisor_warnings = advise(
                        turn.actions, self.config.repo or "", self.pilot
                    )
            except Exception:
                self._pending_advisor_warnings = []

            history_len_before_actions = len(self._history)
            # Track files edited THIS turn (for the auto-verify loop below).
            turn_changed_files: list[str] = []
            # Bulletproof same-turn dedupe: twin run_implement tool_calls with
            # near-identical goals never both reach dispatch.
            turn.actions = dedupe_dispatch_actions(turn.actions)
            for idx, act in enumerate(turn.actions):
                if idx > 0:
                    yield from self._check_and_inject_steer()
                    if self._steer_pending:
                        # A user steer arrived mid-spree. Abandon the REMAINING queued
                        # actions and loop back to re-ask the model, which now sees the
                        # steer as its current instruction. This is what makes a steer
                        # actually interrupt instead of being ignored until the spree ends.
                        break
                if self._cancel.is_set():
                    yield ConvEvent("interrupted", {"reason": "session interrupted"})
                    return
                action_seq += 1
                aid = f"a{action_seq}"
                # Malformed/truncated tool call: do NOT silently drop it. Surface the error
                # back to the model so it re-issues the call with all required arguments, and
                # count it as activity so the autonomous loop does not mistake it for "done".
                if is_invalid_action(act):
                    err = act.content or f"invalid tool call '{act.tool}'"
                    yield ConvEvent("action_result", {"id": aid, "error": err})
                    self._append_action_result(act, aid, err, is_native)
                    turn_had_invalid = True
                    continue
                act_goal = action_display_goal(act)

                # run_implement / run_parallel emit their own action_start after
                # engine selection (includes mode=agentic|native). Emitting here
                # too produced twin "Investigated 2 run implements" chrome.
                if act.kind not in ("run_implement", "run_parallel"):
                    yield ConvEvent("action_start", {
                        "id": aid, "kind": act.kind, "goal": act_goal or act.tool,
                        "cwd": self.config.repo or None,
                        "adapter": self.config.swarm_adapter,
                    })

                if plan and act.kind in ("run_implement", "run_parallel", "write_file", "edit_file", "hash_edit", "run_command"):
                    if act.kind in ("run_implement", "run_parallel"):
                        yield ConvEvent("action_start", {
                            "id": aid, "kind": act.kind, "goal": act_goal or act.tool,
                            "cwd": self.config.repo or None,
                        })
                    yield ConvEvent("action_result", {
                        "id": aid,
                        "error": f"(plan mode: skipped {act.kind})"
                    })
                    self._append_action_result(act, aid, f"(plan mode: skipped {act.kind})", is_native)
                    continue

                if getattr(self.config, "no_delegation", False) and act.kind in ("run_implement", "run_parallel", "run_swarm"):
                    if act.kind in ("run_implement", "run_parallel"):
                        yield ConvEvent("action_start", {
                            "id": aid, "kind": act.kind, "goal": act_goal or act.tool,
                            "cwd": self.config.repo or None,
                        })
                    err_msg = "delegation is disabled for workers; edit the files directly with write_file, edit_file, or hash_edit"
                    yield ConvEvent("action_result", {
                        "id": aid,
                        "error": err_msg
                    })
                    self._append_action_result(act, aid, err_msg, is_native)
                    continue

                # Never let the pilot POST /api/restart mid-turn (drops SSE).
                if act.kind == "run_command":
                    restart_verdict = check_backend_restart(guard_state, act.kind, act)
                    if restart_verdict.suppress:
                        _diag_note(
                            "pilot_guards",
                            msg=f"{restart_verdict.reason} suppressed {act.kind}: {restart_verdict.message[:200]}",
                        )
                        yield ConvEvent("action_result", {"id": aid, "error": restart_verdict.message})
                        self._append_action_result(act, aid, restart_verdict.message, is_native, ok=False)
                        continue

                # Kernel-force native Puppetmaster verbs: CLI redirect runs every turn
                # (independent of broad-intent gate and other guard kill switches).
                if act.kind == "run_command" and cli_redirect_enabled():
                    cli_verdict = check_cli_redirect(guard_state, act.kind, act)
                    if cli_verdict.suppress:
                        _diag_note(
                            "pilot_guards",
                            msg=f"{cli_verdict.reason} suppressed {act.kind}: {cli_verdict.message[:200]}",
                        )
                        yield ConvEvent("action_result", {"id": aid, "error": cli_verdict.message})
                        self._append_action_result(act, aid, cli_verdict.message, is_native, ok=False)
                        continue

                if guards_active():
                    if idx in guard_suppressed:
                        guard_verdict = guard_suppressed[idx]
                        if getattr(guard_verdict, "replay", False):
                            _diag_note(
                                "pilot_guards",
                                msg=f"{guard_verdict.reason} replayed {act.kind}",
                            )
                            _replay_headline = (
                                "swarm gate redirect already issued"
                                if getattr(guard_verdict, "reason", "") == "swarm_gate_replay"
                                else "cached repeat of identical call"
                            )
                            yield ConvEvent("action_result", {
                                "id": aid,
                                "num": 1,
                                "types": ["cached"],
                                "adapter": "local",
                                "mode": "tool",
                                "artifacts": [{"type": "cached", "headline": _replay_headline}],
                            })
                            self._append_action_result(act, aid, guard_verdict.message, is_native, ok=True)
                            continue
                        _diag_note(
                            "pilot_guards",
                            msg=f"{guard_verdict.reason} suppressed {act.kind}: {guard_verdict.message[:200]}",
                        )
                        yield ConvEvent("action_result", {"id": aid, "error": guard_verdict.message})
                        self._append_action_result(act, aid, guard_verdict.message, is_native, ok=False)
                        continue
                    if idx not in guard_recorded_indices:
                        guard_verdict = check_pilot_guards(guard_state, act.kind, act)
                        if guard_verdict.suppress:
                            if getattr(guard_verdict, "replay", False):
                                try:
                                    record_action_execution(guard_state, act.kind, act)
                                except Exception:
                                    pass
                                _diag_note(
                                    "pilot_guards",
                                    msg=f"{guard_verdict.reason} replayed {act.kind}",
                                )
                                _replay_headline = (
                                    "swarm gate redirect already issued"
                                    if getattr(guard_verdict, "reason", "") == "swarm_gate_replay"
                                    else "cached repeat of identical call"
                                )
                                yield ConvEvent("action_result", {
                                    "id": aid,
                                    "num": 1,
                                    "types": ["cached"],
                                    "adapter": "local",
                                    "mode": "tool",
                                    "artifacts": [{"type": "cached", "headline": _replay_headline}],
                                })
                                self._append_action_result(act, aid, guard_verdict.message, is_native, ok=True)
                                continue
                            _diag_note(
                                "pilot_guards",
                                msg=f"{guard_verdict.reason} suppressed {act.kind}: {guard_verdict.message[:200]}",
                            )
                            yield ConvEvent("action_result", {"id": aid, "error": guard_verdict.message})
                            self._append_action_result(act, aid, guard_verdict.message, is_native, ok=False)
                            continue
                        record_action_execution(guard_state, act.kind, act)

                # ---- read-only tool-result assembly (prefetch or live) ----
                if act.kind in READ_ONLY_KINDS:
                    yield from dispatch_readonly_action(
                        self, act, idx, aid, prefetch, is_native
                    )
                    continue

                # ---- local tool-result assembly (workspace / mutate / browse / mcp) ----
                if act.kind in LOCAL_ACTION_KINDS:
                    yield from dispatch_local_action(
                        self, act, aid, is_native, turn_changed_files, act_goal=act_goal,
                    )
                    continue

                # ---- swarm branch --------------------------------------------
                if act.kind == "run_swarm":
                    intent = DriverIntent(action="run_swarm", goal=act.goal,
                                          roles=act.roles or None, rationale="pilot")
                    # Sync analysis used to skip tracker registration — chat showed
                    # "N artifacts via agentic" while Swarm Tracker stayed empty.
                    # Pin a local row for the run, then re-key to the store job id.
                    _sync_local_id = f"local-swarm-{aid}"
                    try:
                        self._register_local_job(
                            _sync_local_id, act.goal, role="explore",
                            cwd=self.config.repo or "",
                            engine="agentic",
                        )
                        self._session_job_ids.append(_sync_local_id)
                    except Exception:
                        pass
                    yield ConvEvent("swarm_pending", {
                        "job_ids": [_sync_local_id],
                        "objective": act.goal,
                    })
                    # Run the (blocking, in-process) swarm on a background thread so
                    # the generator can drain live token deltas from the agentic
                    # worker and forward them to the UI, mirroring the pilot's own
                    # chat_stream(on_delta=...) pattern. Inline workers run
                    # sequentially, so deltas belong to one worker at a time.
                    import queue as _queue
                    import threading as _threading
                    _delta_q: "_queue.Queue" = _queue.Queue()

                    _swarm_thread = _threading.Thread(
                        target=stream_swarm,
                        args=(self, intent, _delta_q),
                        daemon=True,
                    )
                    _swarm_thread.start()
                    result = None
                    swarm_error = None
                    while True:
                        msg_kind, msg_val = _delta_q.get()
                        if msg_kind == "delta":
                            wid, dkind, dtext = msg_val
                            yield ConvEvent("worker_delta", {
                                "id": aid, "worker_id": wid, "kind": dkind, "text": dtext,
                            })
                        elif msg_kind == "done":
                            result = msg_val
                            break
                        else:
                            swarm_error = msg_val
                            break
                    if swarm_error is not None:
                        try:
                            self._finish_local_job(
                                _sync_local_id, ok=False,
                                summary=str(swarm_error)[:200],
                                status="failed", engine="agentic",
                            )
                        except Exception:
                            pass
                        yield ConvEvent("action_result", {"id": aid, "error": f"execute: {swarm_error}"})
                        self._append_action_result(act, aid, f"(swarm {aid} failed: {swarm_error})", is_native)
                        continue
                    if result is None:
                        try:
                            self._finish_local_job(
                                _sync_local_id, ok=False,
                                summary="no result", status="failed", engine="agentic",
                            )
                        except Exception:
                            pass
                        yield ConvEvent("action_result", {"id": aid, "error": "execute: no result"})
                        self._append_action_result(act, aid, f"(swarm {aid} failed: no result)", is_native)
                        continue
                    swarms += 1
                    if result.adapter == "demo":
                        demo_swarms += 1
                    auth_failure = getattr(result, "auth_failure", "") or ""
                    if auth_failure:
                        # A provider rejected the key: surface it as its own loud
                        # event so the UI flags a dead/revoked key as the cause,
                        # not a generic "no findings" degrade.
                        yield ConvEvent("swarm_auth_failure", {
                            "id": aid, "job_id": result.job_id, "message": auth_failure,
                        })
                    # Signal-first ordering: a real swarm returns routing +
                    # verification "plumbing" artifacts BEFORE the actual
                    # finding/risk/decision signal. A naive artifacts[:8] slice
                    # was getting entirely consumed by 5 routing + 5 verification
                    # entries, so a swarm that produced a dozen genuine findings
                    # looked like "only verifications, no findings." Hoist signal
                    # to the front and give it real headroom so the pilot always
                    # sees the findings the swarm actually produced.
                    _SIGNAL = {"finding", "risk", "decision"}
                    _all_arts = list(result.artifacts)
                    _signal = [a for a in _all_arts if str(a.get("type")) in _SIGNAL]
                    _plumbing = [a for a in _all_arts if str(a.get("type")) not in _SIGNAL]
                    ordered = _signal + _plumbing
                    # Show ALL signal artifacts (capped generously) plus a little
                    # plumbing for context, rather than a blind first-N slice.
                    digest_arts = (_signal[:20] + _plumbing[:3]) if _signal else _plumbing[:8]
                    yield ConvEvent("action_result", {
                        "id": aid, "job_id": result.job_id, "num": result.num_artifacts,
                        "types": result.artifact_types, "artifacts": ordered[:12],
                        "adapter": result.adapter, "mode": result.mode,
                        "auth_failure": auth_failure,
                    })
                    # Green badge requires real signal — routing/verification-only
                    # "5 artifacts via agentic" in ~3s was lying about success.
                    _has_signal = bool(_signal)
                    _swarm_ok = _has_signal and not auth_failure
                    if auth_failure:
                        _badge_summary = "auth failure"
                    elif _has_signal:
                        _badge_summary = (
                            f"{len(_signal)} findings via {result.adapter}"
                            f" ({result.num_artifacts} artifacts)"
                        )
                    elif result.num_artifacts:
                        _badge_summary = (
                            f"degraded: {result.num_artifacts} plumbing artifacts "
                            f"via {result.adapter}, no findings"
                        )
                    else:
                        _badge_summary = "no artifacts produced"
                    _badge_error = auth_failure or (
                        None if _swarm_ok else (
                            "swarm produced no FINDING/RISK/DECISION artifacts"
                            if result.num_artifacts else "swarm produced no artifacts"
                        )
                    )
                    _store_jid = (result.job_id or "").strip() or _sync_local_id
                    _badge = {
                        "job_id": _store_jid,
                        "applied": _swarm_ok,
                        "files": [],
                        "summary": _badge_summary,
                        "error": _badge_error,
                        "objective": act.goal,
                    }
                    try:
                        self._finish_local_job(
                            _sync_local_id,
                            ok=_swarm_ok,
                            summary=_badge_summary,
                            status="done" if _swarm_ok else "failed",
                            engine=(result.adapter or "agentic"),
                        )
                        if _store_jid != _sync_local_id:
                            if _store_jid not in self._session_job_ids:
                                self._session_job_ids.append(_store_jid)
                            # Terminal store-keyed row so expand/artifacts resolve.
                            self._register_local_job(
                                _store_jid, act.goal, role="explore",
                                cwd=self.config.repo or "",
                                engine=(result.adapter or "agentic"),
                            )
                            self._finish_local_job(
                                _store_jid,
                                ok=_swarm_ok,
                                summary=_badge_summary,
                                status="done" if _swarm_ok else "failed",
                                engine=(result.adapter or "agentic"),
                            )
                    except Exception:
                        pass
                    self._display_transcript.append({"type": "swarm_result", **_badge})
                    yield ConvEvent("swarm_result", {
                        "job_id": _badge["job_id"],
                        "objective": act.goal,
                        "result": _badge,
                    })
                    # collect non-substrate findings for durable knowledge capture
                    if result.adapter != "demo":
                        turn_findings.extend(
                            a for a in result.artifacts if a.get("type") != "verification")
                    # 5. Feed DISTILLED artifacts back into the transcript (not raw files).
                    digest = "\n".join(f"  - [{a['type']}] {a['headline']}"
                                       for a in digest_arts) or "  (no artifacts)"
                    stall = ""
                    if demo_swarms >= 2:
                        stall = ("\n(NOTE: swarms are running on the DEMO substrate, which "
                                 "returns generic artifacts -- not real codebase analysis. "
                                 "Do NOT keep retrying; explain this to the user and finish "
                                 "with no actions. Real analysis needs --repo + "
                                 "--swarm-adapter openai.)")
                    if auth_failure:
                        # Put the auth failure at the TOP of what the pilot reads and
                        # tell it plainly not to keep retrying a dead key -- the fix
                        # is to repair the credential, not to re-swarm.
                        stall = (f"\n(PROVIDER AUTH FAILURE -- {auth_failure} This is a "
                                 "dead/revoked/wrong API key, NOT a weak model or bad "
                                 "prompt. Do NOT re-run the swarm; tell the user to fix "
                                 "the named key, then stop.)") + stall
                    elif not _has_signal:
                        stall = (
                            "\n(DEGRADED SWARM — only routing/verification plumbing, "
                            "no FINDING/RISK/DECISION. Tell the user the audit did not "
                            "produce real findings. Re-dispatch with fewer roles or a "
                            "sharper goal; do NOT claim the repo was reviewed.)"
                        ) + stall
                    self._append_action_result(act, aid, f"(swarm {aid} '{act.goal}' returned {result.num_artifacts} artifacts via {result.adapter}:\n{digest}\nExplain these findings to the user and either run a narrowed follow-up swarm or finish with no actions.){stall}", is_native)
                    continue

                # ---- run_implement branch ------------------------------------
                if act.kind == "run_implement":
                    # Optional per-dispatch target repo -- lets the pilot point a
                    # single run_implement at a DIFFERENT git repo than the open
                    # workspace. Validated up front; an invalid path surfaces as
                    # an explicit error (no silent fallback to self.config.repo).
                    _target_repo_override = ""
                    if (getattr(act, "repo", "") or "").strip():
                        _abs, _err = self._validate_target_repo(act.repo)
                        if _err:
                            error_msg = f"run_implement: target repo {act.repo} is not a valid git repository"
                            yield ConvEvent("action_start", {
                                "id": aid, "kind": "run_implement", "goal": act.goal,
                                "cwd": self.config.repo or None,
                            })
                            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                            self._append_action_result(act, aid, f"(run_implement {aid} failed: {error_msg})", is_native)
                            continue
                        _target_repo_override = _abs
                    effective_repo = _target_repo_override or self.config.repo
                    if not effective_repo:
                        error_msg = "No workspace directory (config.repo) is open."
                        yield ConvEvent("action_start", {
                            "id": aid, "kind": "run_implement", "goal": act.goal,
                            "cwd": None,
                        })
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(run_implement {aid} failed: {error_msg})", is_native)
                        continue

                    # Hermes-style soft refuse: never dispatch a background
                    # worker that dies instantly on non-git / Home workspaces.
                    try:
                        from harness.implement_guards import check_implement_workspace
                        git_msg = check_implement_workspace(
                            effective_repo, goal=act.goal or "",
                        )
                    except Exception:
                        git_msg = None
                    if git_msg:
                        yield ConvEvent("action_start", {
                            "id": aid, "kind": "run_implement", "goal": act.goal,
                            "cwd": effective_repo,
                        })
                        yield ConvEvent("action_result", {"id": aid, "error": git_msg})
                        self._append_action_result(
                            act, aid,
                            f"(run_implement {aid} refused: {git_msg})",
                            is_native,
                        )
                        continue

                    # Hard fan-out: refuse one-worker rewrites of oversized files.
                    try:
                        from harness.implement_guards import check_oversized_single_file_rewrite
                        fanout_msg = check_oversized_single_file_rewrite(act.goal, effective_repo)
                    except Exception:
                        fanout_msg = None
                    if fanout_msg:
                        yield ConvEvent("action_start", {
                            "id": aid, "kind": "run_implement", "goal": act.goal,
                            "cwd": effective_repo,
                        })
                        yield ConvEvent("action_result", {"id": aid, "error": fanout_msg})
                        self._append_action_result(
                            act, aid,
                            f"(run_implement {aid} refused by fan-out guard: {fanout_msg})",
                            is_native,
                        )
                        continue

                    # Claim BEFORE external vs local branch so a twin run_implement
                    # in the same turn (e.g. cursor + agentic) cannot both dispatch.
                    # Previously only the local path claimed, which produced twin
                    # Swarm Tracker cards for the same goal.
                    if not self._claim_objective(act.goal):
                        dedup_msg = (
                            "An identical objective is already running in a "
                            "background worker -- not dispatching a duplicate. "
                            "Wait for the in-flight worker's patch instead of "
                            "re-issuing the same edit; duplicate workers race the "
                            "same files and cause PATCH-DID-NOT-APPLY."
                        )
                        yield ConvEvent("action_start", {
                            "id": aid, "kind": "run_implement", "goal": act.goal,
                            "cwd": effective_repo,
                        })
                        yield ConvEvent("action_result", {
                            "id": aid, "status": "skipped", "message": dedup_msg,
                        })
                        self._append_action_result(
                            act, aid,
                            f"(run_implement {aid} skipped -- duplicate objective already in flight)",
                            is_native,
                        )
                        continue

                    claimed = True
                    dispatched = False

                    external_adapters = {"cursor", "claude-code", "codex", "openai", "hermes"}
                    requested_adapter, adapter_remap_note = self._resolve_requested_implement_adapter(
                        act.adapter or ""
                    )
                    use_external = (
                        requested_adapter in external_adapters
                        and _puppetmaster_available()
                        and self._external_adapter_available(requested_adapter)
                    )
                    if requested_adapter in external_adapters and not use_external:
                        # Disabled by platform lock or CLI missing -- stay on
                        # agentic/native rather than hard-failing.
                        if not adapter_remap_note:
                            adapter_remap_note = (
                                f"adapter '{requested_adapter}' unavailable; "
                                "using standalone agentic/native"
                            )
                        requested_adapter = ""

                    if use_external:
                        adapter = requested_adapter
                        # External path: no mode= stamp (tests + UI treat mode as
                        # the in-process agentic|native engine label only).
                        yield ConvEvent("action_start", {
                            "id": aid,
                            "kind": "run_implement",
                            "goal": act.goal,
                            "cwd": effective_repo,
                        })
                        try:
                            import json
                            cmd = _puppetmaster_cmd(
                                adapter, act.goal, "--cwd", effective_repo,
                                "--mode", "implement", "--allow-dirty", "--allow-non-worktree",
                                *self._job_dispatch_label_args(),
                            )
                            p = subprocess.Popen(
                                cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True,
                                cwd=effective_repo
                            , encoding="utf-8", errors="replace")

                            job_id = None
                            all_output_lines = []
                            for line in p.stdout:
                                all_output_lines.append(line)
                                if not job_id:
                                    match = re.search(r"\b(job_[a-fA-F0-9]{12})\b", line)
                                    if match:
                                        job_id = match.group(1)

                            p.wait(timeout=600)

                            if job_id:
                                self._session_job_ids.append(job_id)
                                # Submit the await+apply task to the thread pool
                                # through the bounded-inflight gate. If we are at
                                # capacity, refuse to dispatch and tell the pilot
                                # to wait rather than silently queuing more work.
                                if not self._submit_swarm(self._run_swarm_background, job_id, act.goal, None):
                                    cap_msg = (
                                        f"Swarm capacity reached ({self._swarm_inflight()} in flight); "
                                        "not dispatching more right now. Wait for an in-flight worker to finish."
                                    )
                                    self._release_objective(act.goal)
                                    yield ConvEvent("action_result", {"id": aid, "error": cap_msg})
                                    self._append_action_result(act, aid, f"(run_implement {aid} deferred: {cap_msg})", is_native)
                                    continue

                                dispatched = True  # background await owns objective release
                                # Emit ConvEvent kind="swarm_pending" with {job_ids, objective}
                                yield ConvEvent("swarm_pending", {
                                    "job_ids": [job_id],
                                    "objective": act.goal
                                })

                                # Complete the visible action start and result for the dispatch itself
                                yield ConvEvent("action_result", {
                                    "id": aid,
                                    "job_id": job_id,
                                    "status": "pending",
                                    "message": f"Dispatched background swarm job {job_id}"
                                })

                                self._append_action_result(
                                    act, aid,
                                    f"(run_implement {aid} dispatched in background: job {job_id}"
                                    + (f"; {adapter_remap_note}" if adapter_remap_note else "")
                                    + ")",
                                    is_native,
                                )
                                yield from self._answer_remaining_tool_calls(
                                    turn.actions, idx, is_native, action_seq,
                                )
                                yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms + 1})
                                return
                            else:
                                self._release_objective(act.goal)
                                output = "".join(all_output_lines)[:5000]
                                yield ConvEvent("action_result", {
                                    "id": aid,
                                    "error": f"Failed to detect job_id. CLI output:\n{output}"
                                })
                                self._append_action_result(act, aid, f"(run_implement {aid} failed: no job_id detected. Output:\n{output})", is_native)

                        except Exception as e:
                            if claimed and not dispatched:
                                self._release_objective(act.goal)
                            yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                            self._append_action_result(act, aid, f"(run_implement {aid} failed: {e})", is_native)
                        continue
                    else:
                        # Standalone in-process path: the agentic engine (keys-only,
                        # router-picked, no external CLI) by default, or Marionette's
                        # native pilot when no provider key is present / native is asked.
                        from harness.edit_engines import select_edit_engine
                        engine = select_edit_engine(self.config, requested_adapter)
                        # Mode drives whether an empty worktree diff is success
                        # (analysis/review) or failure (implement). Do NOT infer
                        # from prompt keywords -- only the explicit mode field.
                        try:
                            _mode = (getattr(act, "mode", None) or "implement").strip().lower()
                        except Exception:
                            _mode = "implement"
                        if _mode not in ("implement", "analysis", "review"):
                            _mode = "implement"
                        expects_diff = _mode not in ("analysis", "review")
                        yield ConvEvent("action_start", {
                            "id": aid,
                            "kind": "run_implement",
                            "goal": act.goal,
                            "cwd": effective_repo,
                            "mode": engine,
                        })

                        try:
                            import uuid
                            short = uuid.uuid4().hex[:8]
                            job_id = f"local-{short}"
                            self._session_job_ids.append(job_id)
                            # Stamp adapter=engine (agentic|native) at dispatch;
                            # never the pilot driver / openrouter slug.
                            self._register_local_job(
                                job_id, act.goal, role=_mode, cwd=effective_repo,
                                engine=engine,
                                model=(self.config.driver or "") if engine == "native" else "",
                            )

                            # Warm heavy imports single-threaded before the worker
                            # thread races the PyInstaller PYZ reader (see fn docs).
                            _prewarm_worker_imports()
                            # Submit the selected edit engine through the
                            # bounded-inflight gate. At capacity we refuse
                            # rather than queueing unbounded on the executor;
                            # the objective release below happens via the
                            # existing "claimed and not dispatched" cleanup.
                            if not self._submit_swarm(
                                self._run_provider_worker_background,
                                job_id, act.goal, requested_adapter, _target_repo_override,
                                expects_diff,
                            ):
                                cap_msg = (
                                    f"Swarm capacity reached ({self._swarm_inflight()} in flight); "
                                    "not dispatching more right now. Wait for an in-flight worker to finish."
                                )
                                # Nothing was handed to a worker, so release
                                # the objective we just claimed -- otherwise
                                # it leaks and blocks re-issuing the same edit.
                                # (dispatched is still False here.)
                                self._release_objective(act.goal)
                                yield ConvEvent("action_result", {"id": aid, "status": "deferred", "message": cap_msg})
                                self._append_action_result(act, aid, f"(run_implement {aid} deferred: {cap_msg})", is_native)
                                continue
                            dispatched = True  # worker owns the objective release from here

                            # Emit ConvEvent kind="swarm_pending" with {job_ids, objective}
                            yield ConvEvent("swarm_pending", {
                                "job_ids": [job_id],
                                "objective": act.goal
                            })

                            dispatch_msg = f"Dispatched background swarm job {job_id}"
                            if adapter_remap_note:
                                dispatch_msg = f"{dispatch_msg} ({adapter_remap_note})"
                            # Complete the visible action start and result for the dispatch itself
                            yield ConvEvent("action_result", {
                                "id": aid,
                                "job_id": job_id,
                                "status": "pending",
                                "message": dispatch_msg,
                            })

                            self._append_action_result(
                                act, aid,
                                f"(run_implement {aid} dispatched in background: job {job_id}"
                                + (f"; {adapter_remap_note}" if adapter_remap_note else "")
                                + ")",
                                is_native,
                            )
                            yield from self._answer_remaining_tool_calls(
                                turn.actions, idx, is_native, action_seq,
                            )
                            yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms + 1})
                            return
                        except Exception as e:
                            # If we claimed the objective but never handed it to a
                            # worker, release it here -- otherwise it leaks and blocks
                            # all future dispatch of the same work.
                            if claimed and not dispatched:
                                self._release_objective(act.goal)
                            yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                            self._append_action_result(act, aid, f"(run_implement {aid} failed: {e})", is_native)
                        continue

                # ---- run_parallel branch -------------------------------------
                if act.kind == "run_parallel":
                    # Optional per-dispatch target repo (same semantics as
                    # run_implement): validate up front, no silent fallback.
                    _target_repo_override = ""
                    if (getattr(act, "repo", "") or "").strip():
                        _abs, _err = self._validate_target_repo(act.repo)
                        if _err:
                            error_msg = f"run_parallel: target repo {act.repo} is not a valid git repository"
                            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                            self._append_action_result(act, aid, f"(run_parallel {aid} failed: {error_msg})", is_native)
                            continue
                        _target_repo_override = _abs
                    effective_repo = _target_repo_override or self.config.repo
                    if not effective_repo:
                        error_msg = "No workspace directory (config.repo) is open."
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(run_parallel {aid} failed: {error_msg})", is_native)
                        continue

                    goals = act.goals or []
                    if not goals:
                        yield ConvEvent("action_result", {"id": aid, "error": "run_parallel requires a non-empty goals array"})
                        self._append_action_result(act, aid, f"(run_parallel {aid} failed: run_parallel requires a non-empty goals array)", is_native)
                        continue

                    # Soft refuse whole parallel batch on non-git / Home workspace.
                    try:
                        from harness.implement_guards import check_implement_workspace
                        git_msg = check_implement_workspace(
                            effective_repo,
                            goal="; ".join(goals[:3]),
                        )
                    except Exception:
                        git_msg = None
                    if git_msg:
                        yield ConvEvent("action_result", {"id": aid, "error": git_msg})
                        self._append_action_result(
                            act, aid,
                            f"(run_parallel {aid} refused: {git_msg})",
                            is_native,
                        )
                        continue

                    MAX_PARALLEL_CAP = 8
                    if len(goals) > MAX_PARALLEL_CAP:
                        goals = goals[:MAX_PARALLEL_CAP]

                    # Hard fan-out per goal: drop whole-file oversized rewrites.
                    try:
                        from harness.implement_guards import check_oversized_single_file_rewrite
                        kept_goals = []
                        refused_goals = []
                        for g in goals:
                            msg = check_oversized_single_file_rewrite(g, effective_repo)
                            if msg:
                                refused_goals.append((g, msg))
                            else:
                                kept_goals.append(g)
                        if refused_goals:
                            for g, msg in refused_goals:
                                yield ConvEvent("notice", {
                                    "message": f"Fan-out guard refused goal: {msg}",
                                })
                            goals = kept_goals
                        if not goals:
                            err = (
                                "run_parallel: every goal was refused by the fan-out "
                                "guard (oversized single-file rewrite). Split each "
                                "file into sectioned run_parallel goals."
                            )
                            yield ConvEvent("action_result", {"id": aid, "error": err})
                            self._append_action_result(
                                act, aid, f"(run_parallel {aid} failed: {err})", is_native,
                            )
                            continue
                    except Exception:
                        pass

                    external_adapters = {"cursor", "claude-code", "codex", "openai", "hermes"}
                    requested_adapter, adapter_remap_note = self._resolve_requested_implement_adapter(
                        act.adapter or ""
                    )
                    use_external = (
                        requested_adapter in external_adapters
                        and _puppetmaster_available()
                        and self._external_adapter_available(requested_adapter)
                    )
                    if requested_adapter in external_adapters and not use_external:
                        if not adapter_remap_note:
                            adapter_remap_note = (
                                f"adapter '{requested_adapter}' unavailable; "
                                "using standalone agentic/native"
                            )
                        requested_adapter = ""

                    if use_external:
                        adapter = requested_adapter
                        mode = act.mode or "implement"

                        sub_aids = []
                        for idx, sub_goal in enumerate(goals):
                            sub_aid = f"{aid}_sub_{idx}"
                            sub_aids.append(sub_aid)
                            yield ConvEvent("action_start", {
                                "id": sub_aid,
                                "kind": f"run_{mode}",
                                "goal": sub_goal,
                                "cwd": effective_repo
                            })

                        import json
                        import threading
                        import tempfile
                        import shutil
                        processes = []
                        threads = []

                        for idx, sub_goal in enumerate(goals):
                            sub_aid = sub_aids[idx]
                            try:
                                state_dir = tempfile.mkdtemp(prefix="pmh-par-")
                            except Exception as e:
                                yield ConvEvent("action_result", {"id": sub_aid, "error": f"Failed to create temp state-dir: {e}"})
                                continue

                            cmd = _puppetmaster_cmd(
                                "--state-dir", state_dir, adapter, sub_goal,
                                "--cwd", effective_repo, "--mode", mode,
                                "--allow-dirty", "--allow-non-worktree",
                                *self._job_dispatch_label_args(),
                            )
                            try:
                                proc = subprocess.Popen(
                                    cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True,
                                    cwd=effective_repo
                                , encoding="utf-8", errors="replace")
                                p_info = {
                                    "proc": proc,
                                    "goal": sub_goal,
                                    "id": sub_aid,
                                    "job_id": None,
                                    "lines": [],
                                    "state_dir": state_dir
                                }
                                processes.append(p_info)
                                t = threading.Thread(target=read_stdout_thread, args=(p_info,), daemon=True)
                                t.start()
                                threads.append(t)
                            except Exception as e:
                                yield ConvEvent("action_result", {"id": sub_aid, "error": f"Failed to start: {e}"})
                                shutil.rmtree(state_dir, ignore_errors=True)

                        for p_info in processes:
                            try:
                                p_info["proc"].wait(timeout=600)
                            except subprocess.TimeoutExpired:
                                p_info["proc"].kill()
                                p_info["proc"].wait()

                        for t in threads:
                            t.join(timeout=5)

                        aggregate_artifacts_summary = []
                        job_ids_collected = []
                        aggregate_num_artifacts = 0
                        worker_statuses = []

                        for idx, p_info in enumerate(processes):
                            sub_aid = p_info["id"]
                            sub_goal = p_info["goal"]
                            state_dir = p_info.get("state_dir")

                            try:
                                job_id = p_info["job_id"]

                                if not job_id and state_dir:
                                    try:
                                        last_cmd = _puppetmaster_cmd("--state-dir", state_dir, "last")
                                        last_p = subprocess.run(last_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=10)
                                        if last_p.returncode == 0:
                                            last_out = last_p.stdout or ""
                                            m = re.search(r"\b(job_[a-fA-F0-9]{12})\b", last_out)
                                            if m:
                                                p_info["job_id"] = m.group(1)
                                                job_id = p_info["job_id"]
                                    except Exception:
                                        pass

                                if job_id:
                                    # Bounded-inflight gate: if the pool is
                                    # full, refuse this sub-goal's follow-up
                                    # worker rather than piling more onto the
                                    # executor. The CLI subprocess has already
                                    # run at this point, so we surface a notice
                                    # and leave state_dir for the local finally
                                    # block to clean up.
                                    if not self._submit_swarm(self._run_swarm_background, job_id, sub_goal, state_dir):
                                        cap_msg = (
                                            f"Swarm capacity reached ({self._swarm_inflight()} in flight); "
                                            f"not dispatching follow-up for job {job_id}."
                                        )
                                        yield ConvEvent("action_result", {"id": sub_aid, "status": "deferred", "message": cap_msg})
                                        aggregate_artifacts_summary.append(f"Sub-worker for '{sub_goal}' deferred: {cap_msg}")
                                        continue

                                    job_ids_collected.append(job_id)
                                    self._session_job_ids.append(job_id)

                                    # Prevent cleanup of state_dir in local finally block by setting p_info["state_dir"] = None
                                    p_info["state_dir"] = None

                                    yield ConvEvent("action_result", {
                                        "id": sub_aid,
                                        "job_id": job_id,
                                        "status": "pending",
                                        "message": f"Dispatched parallel background swarm job {job_id}"
                                    })
                                else:
                                    ret_code = p_info["proc"].returncode
                                    output_text = "".join(p_info["lines"])
                                    lower_out = output_text.lower()
                                    has_success_marker = any(m in lower_out for m in ["success", "complete", "finished", "done", "written", "saved"])

                                    if ret_code != 0:
                                        err_msg = f"worker process failed (exit {ret_code})"
                                    elif has_success_marker:
                                        err_msg = "worker completed but job_id unrecoverable"
                                    else:
                                        err_msg = "worker completed but job_id unrecoverable (no success marker found)"

                                    yield ConvEvent("action_result", {"id": sub_aid, "error": err_msg})
                                    aggregate_artifacts_summary.append(f"Sub-worker for '{sub_goal}' failed: {err_msg}")
                            finally:
                                if p_info.get("state_dir"):
                                    import shutil
                                    shutil.rmtree(p_info["state_dir"], ignore_errors=True)

                        if job_ids_collected:
                            yield ConvEvent("swarm_pending", {
                                "job_ids": job_ids_collected,
                                "objective": f"Parallel wave of goals: {', '.join(goals)}"
                            })
                            yield ConvEvent("action_result", {
                                "id": aid,
                                "job_id": ",".join(job_ids_collected),
                                "status": "pending",
                                "message": f"Dispatched parallel background swarm jobs: {', '.join(job_ids_collected)}"
                            })
                            self._append_action_result(act, aid, f"(run_parallel dispatched {len(job_ids_collected)} jobs in background: {', '.join(job_ids_collected)})", is_native)
                            yield from self._answer_remaining_tool_calls(
                                turn.actions, idx, is_native, action_seq,
                            )
                            yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms + len(job_ids_collected)})
                            return
                        else:
                            yield ConvEvent("action_result", {
                                "id": aid,
                                "error": "No jobs successfully dispatched"
                            })
                            self._append_action_result(act, aid, f"(run_parallel failed to dispatch any jobs)", is_native)
                        continue
                    else:
                        # Standalone in-process parallel path: the agentic engine per
                        # goal (keys-only, router-picked) or the native pilot fallback.
                        from harness.edit_engines import select_edit_engine
                        engine = select_edit_engine(self.config, requested_adapter)
                        try:
                            _mode = (getattr(act, "mode", None) or "implement").strip().lower()
                        except Exception:
                            _mode = "implement"
                        if _mode not in ("implement", "analysis", "review"):
                            _mode = "implement"
                        expects_diff = _mode not in ("analysis", "review")
                        yield ConvEvent("action_start", {
                            "id": aid,
                            "kind": "run_parallel",
                            "goals": goals,
                            "cwd": effective_repo,
                            "mode": engine,
                        })

                        try:
                            import uuid
                            # Warm heavy imports single-threaded BEFORE fanning out
                            # parallel worker threads, so they never race the
                            # PyInstaller PYZ archive reader (see fn docs).
                            _prewarm_worker_imports()
                            job_ids_collected = []
                            skipped_goals = []
                            deferred_goals = []
                            for sub_goal in goals:
                                # Dedup within the wave AND against already in-flight
                                # objectives: a duplicate worker only races the same
                                # files (audit finding #2). Skip, don't dispatch.
                                if not self._claim_objective(sub_goal):
                                    skipped_goals.append(sub_goal)
                                    continue
                                short = uuid.uuid4().hex[:8]
                                job_id = f"local-{short}"
                                try:
                                    self._register_local_job(
                                        job_id, sub_goal, role=_mode, cwd=effective_repo,
                                        engine=engine,
                                        model=(self.config.driver or "") if engine == "native" else "",
                                    )
                                    # Submit the selected edit engine through the
                                    # bounded-inflight gate. A False return means
                                    # the pool is at capacity: release the
                                    # objective, record a deferred goal, and move on.
                                    submitted = self._submit_swarm(
                                        self._run_provider_worker_background,
                                        job_id, sub_goal, requested_adapter, _target_repo_override,
                                        expects_diff,
                                    )
                                except Exception:
                                    # Never dispatched -> release so it is not leaked.
                                    self._release_objective(sub_goal)
                                    raise
                                if not submitted:
                                    # Never dispatched -> release so it is not leaked.
                                    self._release_objective(sub_goal)
                                    deferred_goals.append(sub_goal)
                                    continue
                                # Dispatched: the worker now owns the objective release.
                                job_ids_collected.append(job_id)
                                self._session_job_ids.append(job_id)

                            if deferred_goals:
                                # Surface a compact notice so the pilot sees
                                # which goals were rejected by the gate.
                                cap_msg = (
                                    f"Swarm capacity reached ({self._swarm_inflight()} in flight); "
                                    f"deferred {len(deferred_goals)} of {len(goals)} goal(s): "
                                    + ", ".join(deferred_goals)
                                )
                                yield ConvEvent("notice", {"message": cap_msg})

                            if not job_ids_collected:
                                # Every goal was a duplicate already in flight.
                                skip_msg = (
                                    "All parallel objectives are already running in "
                                    "background workers -- nothing new dispatched. Wait "
                                    "for the in-flight workers rather than re-issuing them."
                                )
                                yield ConvEvent("action_result", {
                                    "id": aid, "status": "skipped", "message": skip_msg,
                                })
                                self._append_action_result(act, aid, f"(run_parallel {aid} skipped -- all {len(goals)} objectives already in flight)", is_native)
                                continue

                            # Emit ConvEvent kind="swarm_pending" with {job_ids, objective}
                            yield ConvEvent("swarm_pending", {
                                "job_ids": job_ids_collected,
                                "objective": f"Parallel wave of goals: {', '.join(goals)}"
                            })

                            # Complete the visible action start and result for the dispatch itself
                            yield ConvEvent("action_result", {
                                "id": aid,
                                "job_id": ",".join(job_ids_collected),
                                "status": "pending",
                                "message": f"Dispatched parallel background swarm jobs: {', '.join(job_ids_collected)}"
                            })

                            self._append_action_result(act, aid, f"(run_parallel {aid} dispatched {len(job_ids_collected)} jobs in background: {', '.join(job_ids_collected)})", is_native)
                            yield from self._answer_remaining_tool_calls(
                                turn.actions, idx, is_native, action_seq,
                            )
                            yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms + len(job_ids_collected)})
                            return
                        except Exception as e:
                            yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                            self._append_action_result(act, aid, f"(run_parallel {aid} failed: {e})", is_native)
                        continue

                # ---- route_task branch ---------------------------------------
                if act.kind == "route_task":
                    if not _puppetmaster_available():
                        error_msg = "puppetmaster CLI not available in this environment"
                        yield ConvEvent("action_result", {"id": aid, "error": error_msg})
                        self._append_action_result(act, aid, f"(route_task {aid} failed: {error_msg})", is_native)
                        continue

                    instruction = act.instruction or act.arguments.get("instruction") or ""
                    role = act.arguments.get("role") or "explore"

                    try:
                        import json
                        cmd = _puppetmaster_cmd("route", instruction, "--role", role, "--json")
                        p = subprocess.run(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=60
                        )
                        output = p.stdout or ""
                        if p.returncode != 0:
                            raise Exception(f"Exit code {p.returncode}: {output}")

                        route_data = json.loads(output)
                        model_id = route_data.get("model_id") or "unknown"
                        adapter = route_data.get("adapter") or "unknown"
                        cost = route_data.get("nominal_cost_usd", 0.0) or route_data.get("estimated_cost_usd", 0.0)
                        reason = route_data.get("reason") or "No reasoning provided."

                        res_str = (
                            f"**Routed Model**: {model_id} (via {adapter})\n"
                            f"**Estimated Cost**: ${cost:.6f}\n"
                            f"**Reasoning**: {reason}"
                        )

                        yield ConvEvent("action_result", {
                            "id": aid,
                            "num": 1,
                            "types": ["route_task"],
                            "adapter": "local",
                            "mode": "tool",
                            "artifacts": [{"type": "route_task", "headline": f"Routed to {model_id} (${cost:.6f})"}]
                        })
                        self._append_action_result(act, aid, f"(route_task for '{instruction}' returned):\n{res_str}", is_native)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                        self._append_action_result(act, aid, f"(route_task for '{instruction}' failed: {e})", is_native)
                    continue

                # ---- memory branch -------------------------------------------
                if act.kind == "memory":
                    try:
                        op = act.memory_action
                        if op == "add":
                            # Never persist mid-turn. Autopilot: refuse. Interactive:
                            # queue for a Save/Skip card after assistant_done.
                            if self._auto_mode:
                                res_str = (
                                    "Memory add ignored: durable-memory proposals are "
                                    "disabled in Autopilot (unattended). Use Settings > "
                                    "Agent Memory for manual adds, or run interactively."
                                )
                            else:
                                text = (act.memory_content or "").strip()
                                cat = (act.memory_category or "general").strip() or "general"
                                if not text:
                                    raise ValueError("memory add requires content")
                                # Dedupe against already-queued text this turn.
                                already = any(
                                    (q.get("text") or "").strip().lower() == text.lower()
                                    for q in self._turn_memory_queue
                                )
                                if already:
                                    res_str = (
                                        f"Already queued for end-of-turn Save/Skip: '{text}' "
                                        f"(category: {cat}). Not persisted yet."
                                    )
                                else:
                                    self._turn_memory_queue.append({
                                        "text": text,
                                        "category": cat,
                                    })
                                    res_str = (
                                        f"Queued for end-of-turn Save/Skip (not persisted yet): "
                                        f"'{text}' (category: {cat}). The user will confirm after "
                                        f"this turn finishes."
                                    )
                        elif op == "remove":
                            ok = self._memory.remove(act.memory_id)
                            if ok:
                                res_str = f"Successfully removed memory entry with ID {act.memory_id}."
                            else:
                                res_str = f"Error: memory entry with ID {act.memory_id} not found."
                        elif op == "update":
                            ok = self._memory.update(act.memory_id, act.memory_content)
                            if ok:
                                res_str = f"Successfully updated memory entry {act.memory_id} to: '{act.memory_content}'"
                            else:
                                res_str = f"Error: memory entry with ID {act.memory_id} not found."
                        elif op == "list":
                            entries = self._memory.list()
                            if entries:
                                items = "\n".join(f"- [{e.id}] ({e.category}): {e.text}" for e in entries)
                                res_str = f"Durable memory entries:\n{items}"
                            else:
                                res_str = "Durable memory is empty."
                        else:
                            raise ValueError(f"Unknown memory action: {op}")

                        yield ConvEvent("action_result", {
                            "id": aid,
                            "num": 1,
                            "types": ["memory"],
                            "adapter": "local",
                            "mode": "tool",
                            "artifacts": [{"type": "memory", "headline": f"Memory {op} succeeded"}]
                        })
                        self._append_action_result(act, aid, res_str, is_native)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": str(e)})
                        self._append_action_result(act, aid, f"(memory tool execution failed: {e})", is_native)
                    continue

            # Enforce turn budget on the newly appended actions
            new_messages = self._history[history_len_before_actions:]
            self._turn_economy.enforce_tool_batch(new_messages)
            self._history[history_len_before_actions:] = new_messages

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

