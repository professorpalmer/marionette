/**
 * Shared live-SSE + mid-turn chatEvents reattach applicator.
 * Conversation.tsx supplies chrome setters / refs; item transforms live in streamApply.
 */

import type { Dispatch, SetStateAction } from "react";
import type { Card, Item } from "../TranscriptList";
import {
  upsertStreamingThinking,
  upsertToolPrep,
} from "./thinkingToolPrep";
import {
  appendActionStartCard,
  appendAuthFailure,
  appendAutoHalt,
  appendAutoStatus,
  appendCheckpoint,
  appendCodegraphContext,
  appendCommandApproval,
  appendCommandBlocked,
  appendCompaction,
  appendNonStreamingThinking,
  appendQueuedPromptUserBubble,
  appendStreamError,
  appendSwarmPending,
  ensureAssistantStreamingBubble,
  ensureWorkerStreamingBubble,
  failSwarmPendingForActionError,
  finalizeOrphanSwarmPills,
  finalizePilotMessage,
  finalizeStreamingBubbleOnActionResult,
  formatDistilledNotice,
  formatWikiAutoIngestNotice,
  sealOpenStreamSurfaces,
  shouldPaintThinking,
  truncateWaitHint,
  workspaceRootFromActionResult,
} from "./streamApply";
import {
  finalizeOpenPilotBubble,
  sealedAssistantCoversDelta,
} from "./streamBubbles";
import { turnHasLiveInvestigation } from "../../lib/turnProgress";

export type StreamEvent = { kind: string; data?: any };

export type MemoryProposal = { id: string; text: string; category: string };

export type ApplyStreamEventDeps = {
  setCompactingStatus: (v: string | null) => void;
  setItems: Dispatch<SetStateAction<Item[]>>;
  setDistillNotice: Dispatch<SetStateAction<string | null>>;
  setWikiPrepared: Dispatch<
    SetStateAction<{ pages: any[]; autoIngested: boolean } | null>
  >;
  setMemoryProposals: Dispatch<SetStateAction<MemoryProposal[]>>;
  setWaitHint: Dispatch<SetStateAction<string | null>>;
  setStatus: Dispatch<
    SetStateAction<"idle" | "thinking" | "executing" | "done" | "error" | "streaming">
  >;
  setTurnOpen: Dispatch<SetStateAction<boolean>>;
  setPendingJobIds: Dispatch<SetStateAction<string[]>>;
  /** Current pending swarm job ids (kept as a ref so turn-close can read live). */
  pendingJobIdsRef: { current: string[] };
  setSafeTimeout: (fn: () => void, ms: number) => void;
  itemsRef: { current: Item[] };
  planTurnRef: { current: boolean };
  turnSettledRef: { current: boolean };
  resumeQueuedRef: { current: boolean };
  typeBufRef: { current: string };
  flushTypewriter: () => void;
  startTypewriter: () => void;
  appendStreamingText: (chunk: string) => void;
  setCard: (id: string, patch: Partial<Card>) => void;
  onArtifacts: (a: { type: string; headline: string }[]) => void;
  onJobChange: () => void;
  handleSwarmResult: (d: any) => void;
  refreshQueue: () => void;
  fetchContextUsage: () => void;
};

export function createApplyStreamEvent(deps: ApplyStreamEventDeps) {
  const {
    setCompactingStatus,
    setItems,
    setDistillNotice,
    setWikiPrepared,
    setMemoryProposals,
    setWaitHint,
    setStatus,
    setTurnOpen,
    setPendingJobIds,
    pendingJobIdsRef,
    setSafeTimeout,
    itemsRef,
    planTurnRef,
    turnSettledRef,
    resumeQueuedRef,
    typeBufRef,
    flushTypewriter,
    startTypewriter,
    appendStreamingText,
    setCard,
    onArtifacts,
    onJobChange,
    handleSwarmResult,
    refreshQueue,
    fetchContextUsage,
  } = deps;

  return (ev: StreamEvent) => {
    const d = ev.data || {};
    if (ev.kind === "compacting") {
      setCompactingStatus(d.message || "Summarizing chat context");
    } else if (ev.kind === "command_blocked") {
      setItems((p) => appendCommandBlocked(p, d));
    } else if (ev.kind === "command_approval_pending") {
      setItems((p) => appendCommandApproval(p, d));
    } else if (ev.kind === "swarm_auth_failure") {
      // A provider rejected the API key. Surface it as a loud, persistent
      // banner so a dead/revoked key is never silently read as a generic
      // "completed without findings" degrade. Deduped by action id.
      setItems((p) => appendAuthFailure(p, d.message || "", d.id));
    } else if (ev.kind === "wiki_prepared") {
      const pages = d.pages || [];
      if (pages.length > 0) {
        if (d.auto_ingested) {
          // Silent-auto mode already ingested -- just a quiet confirmation footnote.
          const notice = formatWikiAutoIngestNotice(pages.length);
          setDistillNotice(notice);
          setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
        } else {
          // Prepare-and-approve: surface the pages for one-click ingest.
          setWikiPrepared({ pages, autoIngested: false });
        }
      }
    } else if (ev.kind === "memory_propose") {
      // Non-blocking Save/Skip after the final answer. Does not affect
      // composer busy state; ignore/dismiss is fine.
      const id = d.id || "";
      const text = (d.text || "").trim();
      if (id && text) {
        setMemoryProposals((prev) => (
          prev.some((p) => p.id === id)
            ? prev
            : [...prev, { id, text, category: d.category || "general" }]
        ));
      }
    } else if (ev.kind === "codegraph_context") {
      setItems((p) => appendCodegraphContext(p, d.symbols || 0, d.query || ""));
    } else if (ev.kind === "compaction") {
      setCompactingStatus(null);
      setItems((p) => appendCompaction(p, d.before_tokens, d.after_tokens));
      window.dispatchEvent(new Event("harness-context-changed"));
    } else if (ev.kind === "notice" && (d.kind === "wait" || !d.kind)) {
      const hint = truncateWaitHint(d.message || "");
      if (hint) setWaitHint(hint);
    } else if (ev.kind === "thinking") {
      // Live reasoning deltas (delta:true) paint mid-turn so GLM/OR token
      // climbs are visible. Full post-answer reasoning dumps (no delta) stay
      // suppressed -- the answer is already on screen.
      setCompactingStatus(null);
      const { painting, chunk } = shouldPaintThinking(d);
      if (!painting) return;
      setStatus((prev) =>
        prev === "streaming" || prev === "executing" ? prev : "thinking"
      );
      // Drain typewriter before sealing so buffered prose cannot orphan after
      // a later tool card (same barrier as tool_prep / action_start).
      flushTypewriter();
      if (d.delta && chunk) {
        // Seal any open pilot bubble first so thinking cannot reopen or
        // re-parent streamed assistant text into a reasoning row.
        setItems((p) => upsertStreamingThinking(finalizeOpenPilotBubble(p), chunk));
      } else if (chunk.trim()) {
        setItems((p) => appendNonStreamingThinking(finalizeOpenPilotBubble(p), chunk));
      }
    } else if (ev.kind === "tool_prep") {
      const name = String(d.name || "").trim();
      const callId = String(d.id || "").trim();
      if (!name && !callId) return;
      setCompactingStatus(null);
      setStatus((prev) =>
        prev === "streaming" || prev === "executing" ? prev : "thinking"
      );
      // Flush buffered typewriter text into the open bubble BEFORE sealing so
      // pre-tool narration stays above the tool card (never orphans after it).
      flushTypewriter();
      // Seal thinking + assistant surfaces; tool cards only ever hold tool data.
      setItems((p) =>
        upsertToolPrep(sealOpenStreamSurfaces(p), name || "tool_call", {
          goal: d.goal != null ? String(d.goal) : undefined,
          id: callId || undefined,
          status: d.status != null ? String(d.status) : undefined,
        })
      );
    } else if (ev.kind === "message_delta") {
      setCompactingStatus(null);
      setStatus("streaming");
      // Ensure a streaming bubble exists. When the turn already has tool
      // cards (Cursor CLI / investigation), paint deltas instantly — the
      // typewriter over an open Investigating fold reads as chat "loading
      // from top to bottom" after hard commands. Bare prose turns still
      // use the cadence typewriter. ensureAssistantStreamingBubble seals
      // open thinking so reasoning stays on its own finalized row.
      const chunk = d.text || "";
      if (!chunk) return;
      // Cover check + bubble open must run inside the state updater so
      // synchronous chatEvents replay / back-to-back SSE never consults an
      // effect-lagged itemsRef and drop a live post-tool delta.
      let skipCovered = false;
      let investigating = false;
      setItems((p) => {
        itemsRef.current = p;
        if (sealedAssistantCoversDelta(p, chunk)) {
          skipCovered = true;
          return p;
        }
        investigating = turnHasLiveInvestigation(p, true);
        const next = ensureAssistantStreamingBubble(p, { isPlan: planTurnRef.current });
        itemsRef.current = next;
        return next;
      });
      if (skipCovered) return;
      if (investigating) {
        flushTypewriter();
        appendStreamingText(chunk);
      } else {
        typeBufRef.current += chunk;
        startTypewriter();
      }
    } else if (ev.kind === "worker_delta") {
      // Live token stream from an inline swarm worker (the agentic adapter).
      // This is an EPHEMERAL preview: it renders in a height-capped, auto-
      // scrolling window (see Bubble) and is dropped when the action finalizes,
      // because the worker's real output arrives as swarm artifacts/summary.
      // Reuse only a prior workerStream bubble -- never merge worker tokens into
      // the pilot's own message bubble, and never let several workers pile into
      // one unbounded permanent bubble.
      if (d.kind === "text" && d.text) {
        setCompactingStatus(null);
        setStatus("streaming");
        setItems((p) => ensureWorkerStreamingBubble(p, { isPlan: planTurnRef.current }));
        typeBufRef.current += (d.text || "");
        startTypewriter();
      }
    } else if (ev.kind === "message") {
      setCompactingStatus(null);
      setStatus("thinking");
      // Drain any queued typed text before finalizing, so the bubble is whole.
      flushTypewriter();
      setItems((p0) => finalizePilotMessage(p0, d.text, {
        isPlan: planTurnRef.current,
        // Backend flags prose already painted via message_delta so we merge
        // into the sealed bubble instead of appending a duplicate after tools.
        streamed: Boolean(d.streamed),
      }));
    } else if (ev.kind === "action_start") {
      setCompactingStatus(null);
      setStatus("executing");
      // Flush typewriter before seal inside appendActionStartCard so buffered
      // prose cannot land after the tool card.
      flushTypewriter();
      // Idempotent: a late/replayed action_start with the same id must not
      // stack another card (session-switch SSE race → infinite Investigated).
      // Default tool cards to collapsed always: they used to mount open while
      // running and snap shut on action_result, which read as a flicker.
      setItems((p) => appendActionStartCard(p, d));
    } else if (ev.kind === "action_result") {
      setCompactingStatus(null);
      setStatus("thinking");
      // The swarm is done: its structured artifacts/summary land below. Drop
      // the ephemeral worker-stream PREVIEW entirely -- do not convert it into
      // a trailing "reasoning" row (that duplicated the answer and burned
      // scroll/attention). A non-worker streaming bubble (the pilot's own
      // narration) is still finalized in place.
      flushTypewriter();
      setItems((p) => {
        let next = finalizeStreamingBubbleOnActionResult(p);
        // Sync run_swarm early failures emit action_result(error) without a
        // swarm_result — flip the matching local-swarm pill off the spinner.
        if (d.error) next = failSwarmPendingForActionError(next, d.id);
        return next;
      });
      if (d.error && d.id) {
        const localId = `local-swarm-${d.id}`;
        setPendingJobIds((ids) => ids.filter((id) => id !== localId));
      }
      // Fallback: if the card carries an auth_failure but the dedicated
      // swarm_auth_failure event was missed, still raise the loud banner so a
      // dead key is never buried in a quiet "completed" card. Deduped by id.
      if (d.auth_failure) {
        setItems((p) => appendAuthFailure(p, d.auth_failure, d.id));
      }
      setCard(d.id, { running: false, open: false, result: d });
      if (d.artifacts && !d.error) onArtifacts(d.artifacts);
      onJobChange();
      setItems((prev) => {
        const cardItem = prev.find((it) => it.kind === "card" && it.card.id === d.id);
        if (
          cardItem
          && cardItem.kind === "card"
          && (cardItem.card.kind === "open_project" || cardItem.card.kind === "relocate_session")
          && !d.error
        ) {
          window.dispatchEvent(new Event("harness-config-changed"));
          // Prefer the resolved path from action_result (workspace_root/path).
          // Card.goal can be "(workspace root)" when relocate used
          // workspace_root without path — that used to skip the left-rail
          // expand/refresh and leave the project invisible until a tab flip.
          const root = workspaceRootFromActionResult(d, cardItem.card.goal);
          if (root && root !== "(workspace root)") {
            window.dispatchEvent(new CustomEvent("harness-session-relocated", {
              detail: { workspace_root: root },
            }));
          }
        }
        return prev;
      });
    } else if (ev.kind === "auto_status") {
      // Progress receipt only — keep turnOpen sticky until auto_halt / Stop.
      setStatus("executing");
      setItems((p) => appendAutoStatus(p, d.cycle || 0, d.snapshot));
    } else if (ev.kind === "distilled") {
      // Only surface self-learning when it produced something WORTH the user's
      // attention -- a newly PROPOSED skill or rule(s). Skips, duplicates, and
      // "insufficient findings" are the 99% case and stay silent (they are not
      // actionable; announcing them is pure noise).
      const notice = formatDistilledNotice(d);
      if (notice) {
        setDistillNotice(notice);
        // Quiet footnote: auto-fade after 8s so it never lingers like a push notif.
        setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
      }
    } else if (ev.kind === "auto_halt") {
      turnSettledRef.current = true;
      setTurnOpen(false);
      setStatus("done");
      setItems((p) => appendAutoHalt(p, d.reason || "", d.snapshot));
    } else if (ev.kind === "swarm_pending") {
      const job_ids = d.job_ids || [];
      setPendingJobIds((p) => [...p, ...job_ids]);
      setItems((p) => appendSwarmPending(p, job_ids, d.objective || ""));
    } else if (ev.kind === "checkpoint") {
      setItems((p) => appendCheckpoint(p, d));
      window.dispatchEvent(new Event("harness-repo-mutated"));
    } else if (ev.kind === "swarm_result") {
      handleSwarmResult(d);
    } else if (ev.kind === "pilot_resume") {
      // A background job finished and the backend injected a continuation into
      // history. Queue a keep-alive turn; it fires from this turn's onDone.
      resumeQueuedRef.current = true;
    } else if (ev.kind === "queued_prompt") {
      // The backend drained one item off the server-side prompt queue and
      // started running it as the next turn IN THE SAME STREAM. It already
      // appended the prompt to history, but the transcript UI never saw a
      // user message for it -- so the queued turn ran invisibly (no "you
      // said X" bubble). Render the user bubble here so the auto-run queued
      // prompt shows up in chat exactly like a normally-sent turn. Then
      // refetch so the chip list drops it and promotes the next item.
      if (d.text) {
        const qImgs: string[] = Array.isArray(d.images) ? d.images : [];
        setItems((p) => appendQueuedPromptUserBubble(p, d.text, qImgs));
      }
      refreshQueue();
    } else if (ev.kind === "assistant_done") {
      turnSettledRef.current = true;
      setTurnOpen(false);
      setWaitHint(null);
      setStatus("done");
      // Drain + seal any remaining live surfaces so a turn cannot close with an
      // open typewriter / streaming bubble still painted as in-flight.
      flushTypewriter();
      // Sync local-swarm-* ids finish inside the turn; anything still spinning
      // for those is an orphan. Background job_*/local-* stay live so their
      // pills keep spinning until swarm_result arrives.
      const liveIds = pendingJobIdsRef.current.filter(
        (id) => !id.startsWith("local-swarm-"),
      );
      setPendingJobIds(liveIds);
      setItems((p) =>
        finalizeOrphanSwarmPills(sealOpenStreamSurfaces(p), liveIds),
      );
      fetchContextUsage();
      // Backend may also set_title_if_default; refresh meters/title if the
      // optimistic first-send rename missed or the server derived a different slug.
      window.dispatchEvent(new Event("harness-config-changed"));
    } else if (ev.kind === "error") {
      turnSettledRef.current = true;
      setTurnOpen(false);
      setCompactingStatus(null);
      setWaitHint(null);
      setStatus("error");
      const liveIds = pendingJobIdsRef.current.filter(
        (id) => !id.startsWith("local-swarm-"),
      );
      setPendingJobIds(liveIds);
      setItems((p) =>
        finalizeOrphanSwarmPills(appendStreamError(p, d.error || ""), liveIds),
      );
    }
  };
}
