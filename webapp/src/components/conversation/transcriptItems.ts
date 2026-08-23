import type { Item, Msg } from "../TranscriptList";
import {
  mergeSwarmPendingItems,
  normalizeSwarmJobIds,
  swarmPendingIdentityKey,
  swarmPendingStatusOf,
} from "./swarmPendingIdentity";
import {
  boundActionField,
  isTerminalJobStatus,
  MAX_ACTION_ERROR_CHARS,
  MAX_ACTION_GOAL_CHARS,
  MAX_ACTION_ID_CHARS,
  MAX_ACTION_KIND_CHARS,
  normalizeNestedActionStatus,
} from "./nestedActionBounds";
import { hoistCardsBeforeTrailingFinals } from "./thinkingToolPrep";

/** Same SHA-256 hex gate as SSE ``appendCommandApproval`` (streamApply). */
const COMMAND_HASH_HEX = /^[0-9a-f]{64}$/;

const TURN_CONTEXT_MARKER = "[context for this turn]";
const CODEGRAPH_INJECTION_PREFIX = "CODEGRAPH HAS ALREADY BEEN QUERIED";

type SwarmResultItem = Extract<Item, { kind: "swarm_result" }>;

/**
 * Latest-authoritative merge for swarm_result reuse provenance.
 * Explicit next fields (including applied=false, reuse_status='fresh',
 * invalidated_paths=[]) replace prior values; only undefined next fields
 * inherit prior provenance.
 */
export function mergeSwarmResultReuse(
  prev: SwarmResultItem,
  next: SwarmResultItem,
): SwarmResultItem {
  return {
    ...prev,
    ...next,
    applied: typeof next.applied === "boolean" ? next.applied : prev.applied,
    files: next.files !== undefined ? (next.files || []) : (prev.files || []),
    summary: next.summary || prev.summary,
    error: next.error !== undefined ? next.error : prev.error,
    objective: next.objective || prev.objective,
    cwd: next.cwd !== undefined ? next.cwd : prev.cwd,
    held_for_review: next.held_for_review !== undefined
      ? next.held_for_review
      : prev.held_for_review,
    analysis_ok: next.analysis_ok !== undefined ? next.analysis_ok : prev.analysis_ok,
    reuse_status: next.reuse_status !== undefined ? next.reuse_status : prev.reuse_status,
    source_job_id: next.source_job_id !== undefined ? next.source_job_id : prev.source_job_id,
    reuse_reason: next.reuse_reason !== undefined ? next.reuse_reason : prev.reuse_reason,
    validation_fingerprint: next.validation_fingerprint !== undefined
      ? next.validation_fingerprint
      : prev.validation_fingerprint,
    environment_fingerprint: next.environment_fingerprint !== undefined
      ? next.environment_fingerprint
      : prev.environment_fingerprint,
    invalidated_paths: next.invalidated_paths !== undefined
      ? next.invalidated_paths
      : prev.invalidated_paths,
    acceptance_criteria: next.acceptance_criteria !== undefined
      ? next.acceptance_criteria
      : prev.acceptance_criteria,
    artifacts: next.artifacts !== undefined ? next.artifacts : prev.artifacts,
    artifact_delivery: next.artifact_delivery !== undefined
      ? next.artifact_delivery
      : prev.artifact_delivery,
  };
}

/**
 * Strip append-only turn-context trailers from user-visible message text.
 * Mirrors harness.conversation.strip_turn_context_trailer — history may keep
 * the injection for the pilot; display/UI must never show it.
 */
export function stripUserVisibleText(text: string): string {
  if (!text) return text;
  const markers = [
    `\n\n${TURN_CONTEXT_MARKER}\n`,
    `\n\n${TURN_CONTEXT_MARKER}`,
    `${TURN_CONTEXT_MARKER}\n`,
    TURN_CONTEXT_MARKER,
  ];
  let cut: number | null = null;
  for (const marker of markers) {
    const idx = text.indexOf(marker);
    if (idx !== -1 && (cut === null || idx < cut)) {
      cut = idx;
    }
  }
  let out = cut !== null ? text.slice(0, cut).replace(/\s+$/, "") : text;
  if (out.trimStart().startsWith(CODEGRAPH_INJECTION_PREFIX)) {
    return "";
  }
  return out;
}

export function getSimilarity(s1: string, s2: string): number {
  const norm1 = s1.toLowerCase().replace(/[^a-z0-9]/g, "");
  const norm2 = s2.toLowerCase().replace(/[^a-z0-9]/g, "");

  if (!norm1 || !norm2) return 0;
  if (norm1 === norm2) return 1.0;

  if (norm1.startsWith(norm2) || norm2.startsWith(norm1)) {
    return 1.0;
  }

  const w1 = s1.toLowerCase().replace(/[^a-z0-9\s]/g, "").split(/\s+/).filter(Boolean);
  const w2 = s2.toLowerCase().replace(/[^a-z0-9\s]/g, "").split(/\s+/).filter(Boolean);
  const set1 = new Set(w1);
  const set2 = new Set(w2);
  let intersect = 0;
  set1.forEach(w => {
    if (set2.has(w)) intersect++;
  });
  const wordJaccard = intersect / (set1.size + set2.size - intersect);

  const getBigrams = (s: string) => {
    const bigrams = new Set<string>();
    for (let i = 0; i < s.length - 1; i++) {
      bigrams.add(s.substring(i, i + 2));
    }
    return bigrams;
  };
  const b1 = getBigrams(norm1);
  const b2 = getBigrams(norm2);
  if (b1.size > 0 && b2.size > 0) {
    let bIntersect = 0;
    b1.forEach(b => {
      if (b2.has(b)) bIntersect++;
    });
    const charJaccard = bIntersect / (b1.size + b2.size - bIntersect);
    return Math.max(wordJaccard, charJaccard);
  }

  return wordJaccard;
}

/**
 * Drop near-duplicate assistant narration within a turn.
 *
 * Pilots often restate the same diagnosis after each tool ("Found the root
 * causes…") with cards between the bubbles -- consecutive-only dedupe missed
 * that and left the user reading the same paragraph twice while tokens burned.
 * Scan back past cards/thinking within the current user turn; keep the longer
 * copy when similarity is high.
 */
export function deduplicateAssistantNarration(items: Item[]): Item[] {
  const result: Item[] = [];
  // Indices into `result` of assistant msgs since the last user msg.
  let turnAssistantIdx: number[] = [];

  for (const item of items) {
    if (item.kind === "msg" && item.msg.role === "user") {
      turnAssistantIdx = [];
      result.push(item);
      continue;
    }

    if (item.kind === "msg" && item.msg.role === "assistant") {
      // Never collapse an open stream into a prior bubble -- the typewriter
      // still owns it; finalize path will re-run this after streaming:false.
      if (item.msg.streaming) {
        result.push(item);
        turnAssistantIdx.push(result.length - 1);
        continue;
      }

      const newText = item.msg.text || "";
      let dupIdx = -1;
      for (let i = turnAssistantIdx.length - 1; i >= 0; i--) {
        const prev = result[turnAssistantIdx[i]];
        if (!prev || prev.kind !== "msg") continue;
        if (prev.msg.streaming) continue;
        if (getSimilarity(prev.msg.text || "", newText) > 0.85) {
          dupIdx = turnAssistantIdx[i];
          break;
        }
      }

      if (dupIdx >= 0) {
        const prev = result[dupIdx] as { kind: "msg"; msg: Msg };
        if (newText.length > (prev.msg.text || "").length) {
          result[dupIdx] = item;
        }
        continue;
      }

      result.push(item);
      turnAssistantIdx.push(result.length - 1);
      continue;
    }

    result.push(item);
  }
  return result;
}

/** @deprecated use deduplicateAssistantNarration -- kept as alias for callers. */
export function deduplicateConsecutiveAssistantMessages(items: Item[]): Item[] {
  return deduplicateAssistantNarration(items);
}

function preferCardOver(prev: Extract<Item, { kind: "card" }>["card"], next: Extract<Item, { kind: "card" }>["card"]): boolean {
  // Poll/SSE interleave: keep the completed / result-bearing row over in-flight.
  if (prev.running && !next.running) return true;
  if (!prev.result && !!next.result) return true;
  return false;
}

/**
 * Collapse tool cards / swarm badges by stable identity (tool call id / job id),
 * regardless of arrival order. Session-switch SSE races and poll/SSE interleave
 * can otherwise leave duplicate Investigated rows forever.
 */
export function dedupeDisplayItems(items: Item[]): Item[] {
  const out: Item[] = [];
  const cardIndexById = new Map<string, number>();
  const swarmIndexById = new Map<string, number>();
  const swarmPendingIndexByKey = new Map<string, number>();
  const approvalIndexByHash = new Map<string, number>();
  for (const item of items) {
    if (item.kind === "card" && item.card?.id) {
      const id = String(item.card.id);
      const prevIdx = cardIndexById.get(id);
      if (prevIdx != null) {
        const prev = out[prevIdx];
        if (prev.kind === "card" && preferCardOver(prev.card, item.card)) {
          out[prevIdx] = item;
        }
        continue;
      }
      cardIndexById.set(id, out.length);
      out.push(item);
      continue;
    }
    if (item.kind === "swarm_pending") {
      // Collapse historical duplicate lifecycle pills by normalized job ids.
      // Objective alone is never a key — distinct jobs sharing a goal stay distinct.
      const key = swarmPendingIdentityKey(item.job_ids);
      if (key) {
        const prevIdx = swarmPendingIndexByKey.get(key);
        if (prevIdx != null) {
          const prev = out[prevIdx];
          if (prev.kind === "swarm_pending") {
            out[prevIdx] = mergeSwarmPendingItems(prev, item);
          }
          continue;
        }
        swarmPendingIndexByKey.set(key, out.length);
      }
      out.push(item);
      continue;
    }
    if (item.kind === "swarm_result" && item.job_id) {
      const id = String(item.job_id);
      const prevIdx = swarmIndexById.get(id);
      if (prevIdx != null) {
        const prev = out[prevIdx];
        if (prev.kind === "swarm_result") {
          // Replace/merge with richer reuse provenance instead of first-wins.
          // Later row is authoritative; merge only fills undefined next fields.
          out[prevIdx] = mergeSwarmResultReuse(prev, item);
        }
        continue;
      }
      swarmIndexById.set(id, out.length);
      out.push(item);
      continue;
    }
    if (item.kind === "command_approval" && item.commandHash) {
      const hash = String(item.commandHash);
      const prevIdx = approvalIndexByHash.get(hash);
      if (prevIdx != null) {
        const prev = out[prevIdx];
        // Keep a terminal decision over a still-pending SSE/poll duplicate.
        if (
          prev.kind === "command_approval"
          && prev.status === "pending"
          && item.status !== "pending"
        ) {
          out[prevIdx] = item;
        }
        continue;
      }
      approvalIndexByHash.set(hash, out.length);
      out.push(item);
      continue;
    }
    out.push(item);
  }
  return out;
}

/** Map /api/sessions/transcript payload into transcript Item rows. */
export function transcriptResponseToItems(res: {
  history?: any[];
  display?: any[];
}): Item[] {
  let loadedItems: Item[] = [];
  if (res.display && res.display.length > 0) {
    loadedItems = res.display.flatMap((m: any): Item[] => {
      if (m.type === "card") {
        if (m.kind === "run_swarm") return [];
        // result == null means still in flight (persisted at action_start).
        const pending = m.result == null;
        const goals = Array.isArray(m.goals)
          ? m.goals.map((g: unknown) => String(g || "")).filter(Boolean)
          : undefined;
        const parentStatus = m.result && typeof m.result === "object"
          ? String((m.result as { status?: unknown }).status || "")
          : "";
        const parentTerminal =
          !pending
          && (
            isTerminalJobStatus(parentStatus)
            || parentStatus === "complete"
            || parentStatus === "interrupted"
            || Boolean((m.result as { error?: unknown } | null)?.error)
          );
        const settleOutcome: "complete" | "failed" =
          parentStatus === "failed"
          || parentStatus === "error"
          || parentStatus === "cancelled"
          || parentStatus === "canceled"
          || parentStatus === "interrupted"
          || Boolean((m.result as { error?: unknown } | null)?.error)
            ? "failed"
            : "complete";
        const actions = Array.isArray(m.actions)
          ? m.actions
            .filter((a: any) => a && typeof a === "object" && a.action_id)
            .map((a: any) => {
              let status = normalizeNestedActionStatus(a.status, a.error);
              // Durable reload: a finished parent must not resurrect nested spinners.
              if (parentTerminal && status === "running") {
                status = settleOutcome;
              }
              return {
                action_id: boundActionField(a.action_id, MAX_ACTION_ID_CHARS),
                kind: boundActionField(a.kind || "tool_call", MAX_ACTION_KIND_CHARS) || "tool_call",
                goal: boundActionField(a.goal || "", MAX_ACTION_GOAL_CHARS),
                status,
                duration_ms: typeof a.duration_ms === "number" ? a.duration_ms : null,
                error: a.error ? boundActionField(a.error, MAX_ACTION_ERROR_CHARS) : "",
                worker_id: a.worker_id ? String(a.worker_id) : undefined,
              };
            })
          : undefined;
        return [{
          kind: "card" as const,
          card: {
            id: m.id,
            goal: m.goal || (goals ? goals.join(", ") : ""),
            cwd: m.cwd || null,
            kind: m.kind,
            call_id: m.call_id ? String(m.call_id) : undefined,
            goals,
            actions,
            worker_id: m.worker_id ? String(m.worker_id) : undefined,
            running: pending,
            open: false,
            result: pending ? undefined : (m.result || undefined)
          }
        }];
      } else if (m.type === "swarm_result") {
        return [{
          kind: "swarm_result" as const,
          job_id: m.job_id || "",
          applied: !!m.applied,
          files: Array.isArray(m.files) ? m.files : [],
          summary: m.summary || "",
          error: m.error || null,
          objective: m.objective || "",
          cwd: m.cwd !== undefined && m.cwd !== null ? String(m.cwd) : undefined,
          held_for_review: m.held_for_review !== undefined && m.held_for_review !== null
            ? Boolean(m.held_for_review)
            : undefined,
          analysis_ok: m.analysis_ok !== undefined && m.analysis_ok !== null
            ? Boolean(m.analysis_ok)
            : undefined,
          // Explicit empty string is an authoritative clear; only absent/null
          // means "field not present" (merge may inherit prior provenance).
          reuse_status: m.reuse_status !== undefined && m.reuse_status !== null
            ? String(m.reuse_status)
            : undefined,
          source_job_id: m.source_job_id !== undefined && m.source_job_id !== null
            ? String(m.source_job_id)
            : undefined,
          reuse_reason: m.reuse_reason !== undefined && m.reuse_reason !== null
            ? String(m.reuse_reason)
            : undefined,
          validation_fingerprint:
            m.validation_fingerprint !== undefined && m.validation_fingerprint !== null
              ? String(m.validation_fingerprint)
              : undefined,
          environment_fingerprint:
            m.environment_fingerprint !== undefined && m.environment_fingerprint !== null
              ? String(m.environment_fingerprint)
              : undefined,
          invalidated_paths: Array.isArray(m.invalidated_paths)
            ? m.invalidated_paths.map((p: unknown) => String(p || "")).filter(Boolean)
            : undefined,
          acceptance_criteria: Array.isArray(m.acceptance_criteria)
            ? m.acceptance_criteria.map((c: unknown) => String(c || "").trim()).filter(Boolean)
            : undefined,
          artifacts: Array.isArray(m.artifacts) ? m.artifacts : undefined,
          artifact_delivery: m.artifact_delivery && typeof m.artifact_delivery === "object"
            ? m.artifact_delivery
            : undefined,
        }];
      } else if (m.type === "secret_request") {
        const connector = String(m.connector || "").trim().toLowerCase();
        const field = String(m.field || "").trim();
        if (!connector || !field) return [];
        const status = (
          m.status === "saved"
          || m.status === "declined"
          || m.status === "saving"
          || m.status === "error"
        ) ? m.status : "pending";
        return [{
          kind: "secret_request" as const,
          id: m.id || `${connector}::${field}`,
          label: m.label || field,
          connector,
          field,
          description: m.description || "",
          sessionId: m.session_id || "",
          status,
        }];
      } else if (m.type === "command_approval") {
        // Reject empty/malformed hashes so hydrate cannot create colliding
        // keys or invalid cards that suppress a later valid approval.
        const commandHash = (m.command_hash || "").trim().toLowerCase();
        if (!COMMAND_HASH_HEX.test(commandHash)) {
          return [];
        }
        const status = (
          m.status === "approved"
          || m.status === "rejected"
          || m.status === "approving"
          || m.status === "error"
        ) ? m.status : "pending";
        return [{
          kind: "command_approval" as const,
          id: m.id || m.action_id || commandHash,
          command: m.command || "",
          commandHash,
          sessionId: m.session_id || "",
          workspaceRoot: m.workspace_root || "",
          category: m.category || "",
          reason: m.reason || "",
          matched: m.matched || "",
          status,
          ...(typeof m.error === "string" && m.error ? { error: m.error } : {}),
        }];
      } else if (m.type === "turn_terminal") {
        const text = String(m.text || "").trim();
        if (!text) return [];
        const id = String(m.id || "").trim();
        return [{
          kind: "turn_terminal" as const,
          id: id || `turn-term-${String(m.cause || "unspecified")}-${String(m.state || "settled_incomplete")}-${text.length}`,
          cause: String(m.cause || "unspecified"),
          state: String(m.state || "settled_incomplete"),
          text,
        }];
      } else {
        const rawText = m.text || "";
        const role = m.role as "user" | "assistant";
        return [{
          kind: "msg" as const,
          msg: {
            role,
            text: role === "user" ? stripUserVisibleText(rawText) : rawText,
          }
        }];
      }
    });
  } else {
    loadedItems = (res.history || [])
      .filter((m: any) => m.role === "assistant" || (m.role === "user" && m.content && !m.content.startsWith("(")))
      .map((m: any) => {
        const role = m.role as "user" | "assistant";
        const rawText = m.content || "";
        return {
          kind: "msg" as const,
          msg: {
            role,
            text: role === "user" ? stripUserVisibleText(rawText) : rawText,
          }
        };
      });
  }
  // Cursor CLI may persist tool cards after a flushed finale; hoist on hydrate
  // so Explored sits above the answer after reload (same as assistant_done).
  return hoistCardsBeforeTrailingFinals(
    deduplicateConsecutiveAssistantMessages(dedupeDisplayItems(loadedItems)),
  );
}

function cardCount(items: Item[]): number {
  let n = 0;
  for (const it of items) if (it.kind === "card") n += 1;
  return n;
}

function runningCardIds(items: Item[]): Set<string> {
  const ids = new Set<string>();
  for (const it of items) {
    if (it.kind === "card" && it.card.running && it.card.id) {
      ids.add(String(it.card.id));
    }
  }
  return ids;
}

/** Keep still-pending local approval cards when remote hydrate omits them. */
function appendMissingPendingApprovals(base: Item[], local: Item[]): Item[] {
  const have = new Set(
    base
      .filter((it): it is Extract<Item, { kind: "command_approval" }> => (
        it.kind === "command_approval"
      ))
      .map((it) => String(it.commandHash || ""))
      .filter(Boolean),
  );
  const out = [...base];
  for (const it of local) {
    if (
      it.kind === "command_approval"
      && it.status === "pending"
      && it.commandHash
      && !have.has(String(it.commandHash))
    ) {
      out.push(it);
      have.add(String(it.commandHash));
    }
  }
  return out;
}

/**
 * True when applying `remote` (sessionTranscript poll) would erase live tool
 * rows the SSE stream already painted -- the Investigating blink / disappear
 * bug while run_command is still going.
 */
export function shouldPreferLocalTranscript(local: Item[], remote: Item[]): boolean {
  const localRunning = runningCardIds(local);
  if (localRunning.size > 0) {
    for (const id of localRunning) {
      const rem = remote.find((it) => it.kind === "card" && it.card.id === id);
      if (!rem) return true;
      // Remote still pending (result null → running) or completed: ok to take remote.
    }
  }
  // Never shrink the tool timeline mid-session; poll payloads can lag saves.
  if (cardCount(local) > cardCount(remote)) return true;
  return false;
}

function isAssistantSurface(it: Item): boolean {
  return (it.kind === "msg" && it.msg.role === "assistant") || it.kind === "thinking";
}

/**
 * 0-based ordinal of the user-turn that owns `index` — the last user message
 * strictly before that index (not the count of users before it).
 */
function owningUserTurnOrdinal(items: Item[], index: number): number {
  let ordinal = -1;
  let n = 0;
  for (let i = 0; i < index && i < items.length; i++) {
    const row = items[i];
    if (row.kind === "msg" && row.msg.role === "user") {
      ordinal = n;
      n += 1;
    }
  }
  return ordinal;
}

/** Bounds of the local turn for the Nth user message (0-based ordinal). */
function localTurnBounds(items: Item[], turnOrdinal: number): { start: number; end: number } {
  let seen = 0;
  let start = 0;
  let found = false;
  for (let i = 0; i < items.length; i++) {
    const row = items[i];
    if (row.kind !== "msg" || row.msg.role !== "user") continue;
    if (seen === turnOrdinal) {
      start = i + 1;
      found = true;
      break;
    }
    seen += 1;
  }
  if (!found) {
    return { start: items.length, end: items.length };
  }
  let end = items.length;
  for (let i = start; i < items.length; i++) {
    const row = items[i];
    if (row.kind === "msg" && row.msg.role === "user") {
      end = i;
      break;
    }
  }
  return { start, end };
}

/** Assistant/thinking surfaces before `cardIndex` inside its remote user-turn. */
function assistantSurfacesBeforeInTurn(items: Item[], cardIndex: number): number {
  let turnStart = 0;
  for (let i = cardIndex - 1; i >= 0; i--) {
    const row = items[i];
    if (row.kind === "msg" && row.msg.role === "user") {
      turnStart = i + 1;
      break;
    }
  }
  let count = 0;
  for (let i = turnStart; i < cardIndex; i++) {
    if (isAssistantSurface(items[i])) count += 1;
  }
  return count;
}

/**
 * Splice index for a missing remote call_id card: same user-turn ordinal and
 * assistant-surface offset as remote, before trailing final assistant prose.
 * Uses call_id chronology — never kind-only matching.
 */
function insertIndexForRemoteCard(
  local: Item[],
  remote: Item[],
  remoteCardIndex: number,
): number {
  const turnOrdinal = owningUserTurnOrdinal(remote, remoteCardIndex);
  if (turnOrdinal < 0) return local.length;
  const surfacesBefore = assistantSurfacesBeforeInTurn(remote, remoteCardIndex);
  const { start: turnStart, end: turnEnd } = localTurnBounds(local, turnOrdinal);
  let cursor = turnStart;
  let seen = 0;
  while (cursor < turnEnd && seen < surfacesBefore) {
    if (isAssistantSurface(local[cursor])) seen += 1;
    cursor += 1;
  }
  // Advance past cards/preps already placed at this slot (remote order).
  while (cursor < turnEnd) {
    const row = local[cursor];
    if (row.kind === "card" || row.kind === "tool_prep") {
      cursor += 1;
      continue;
    }
    break;
  }
  // Park before trailing post-tool assistant/thinking of this turn.
  let insertAt = cursor;
  for (let i = turnEnd - 1; i >= cursor; i--) {
    const row = local[i];
    if (row.kind === "tool_prep") continue;
    if (!isAssistantSurface(row)) break;
    insertAt = i;
  }
  return insertAt;
}

/**
 * Merge a disk/API transcript into the live feed without dropping in-flight
 * cards. Prefer remote message text when ids match; keep local-only cards
 * and still-pending command approval cards. When local cards win the prefer
 * path, remote swarm_result / swarm_pending rows still reconcile by stable
 * identity (latest-explicit reuse merge + terminal pending merge).
 */
/** Keep this session's terminal chip when disk/API hydrate omits client-only rows. */
export function mergeLocalTurnTerminals(remote: Item[], local: Item[]): Item[] {
  const remoteHas = remote.some((it) => it.kind === "turn_terminal");
  if (remoteHas) return remote;
  const localTerms = local.filter(
    (it): it is Extract<Item, { kind: "turn_terminal" }> => it.kind === "turn_terminal",
  );
  if (localTerms.length === 0) return remote;
  return [...remote, ...localTerms];
}

export function mergeTranscriptItems(local: Item[], remote: Item[]): Item[] {
  if (!shouldPreferLocalTranscript(local, remote)) {
    // Equal card counts take remote, but never drop a still-pending approval
    // the SSE stream already painted (ring_miss / cursor_gap hydrate).
    return dedupeDisplayItems(appendMissingPendingApprovals(
      mergeLocalTurnTerminals(remote, local),
      local,
    ));
  }
  const remoteByCardId = new Map<string, Extract<Item, { kind: "card" }>>();
  for (const it of remote) {
    if (it.kind === "card" && it.card.id) {
      remoteByCardId.set(String(it.card.id), it);
    }
  }
  const merged = local.map((it) => {
    if (it.kind !== "card" || !it.card.id) return it;
    const rem = remoteByCardId.get(String(it.card.id));
    if (!rem) return it;
    // Remote finished the tool -- take its result, drop running.
    if (!rem.card.running && rem.card.result) {
      return {
        kind: "card" as const,
        card: {
          ...it.card,
          running: false,
          result: rem.card.result,
          goal: rem.card.goal || it.card.goal,
          kind: rem.card.kind || it.card.kind,
          call_id: rem.card.call_id || it.card.call_id,
          actions: rem.card.actions || it.card.actions,
        },
      };
    }
    return it;
  });
  // Promote remote durable cards into local tool-prep:{call_id} slots in place
  // so reload/hydrate never appends the action beneath later reasoning.
  const consumedRemoteIds = new Set<string>();
  const withSlots = merged.map((it) => {
    if (it.kind !== "card" || !it.card.id) return it;
    const id = String(it.card.id);
    if (!id.startsWith("tool-prep:")) return it;
    const callKey = id.slice("tool-prep:".length);
    if (!callKey) return it;
    for (const rem of remote) {
      if (rem.kind !== "card" || !rem.card.id) continue;
      const remId = String(rem.card.id);
      if (consumedRemoteIds.has(remId)) continue;
      const remCall = String(rem.card.call_id || rem.card.id || "").trim();
      if (remCall !== callKey && remId !== callKey) continue;
      consumedRemoteIds.add(remId);
      return {
        kind: "card" as const,
        card: {
          ...rem.card,
          // Keep the provisional slot's visible metadata when remote is sparse.
          goal: rem.card.goal || it.card.goal,
          kind: rem.card.kind || it.card.kind,
          call_id: rem.card.call_id || callKey,
        },
      };
    }
    return it;
  });
  // Insert remote cards the local feed never saw (reattach gap) using remote
  // turn chronology — never kind-only matching, never after final prose.
  const localIds = new Set(
    withSlots
      .filter((it) => it.kind === "card" && it.card.id)
      .map((it) => String((it as Extract<Item, { kind: "card" }>).card.id)),
  );
  const localCallIds = new Set(
    withSlots
      .filter((it) => it.kind === "card")
      .map((it) => String((it as Extract<Item, { kind: "card" }>).card.call_id || "").trim())
      .filter(Boolean),
  );
  for (let remIdx = 0; remIdx < remote.length; remIdx++) {
    const it = remote[remIdx];
    if (it.kind !== "card" || !it.card.id) continue;
    const remId = String(it.card.id);
    const remCall = String(it.card.call_id || "").trim();
    if (consumedRemoteIds.has(remId) || localIds.has(remId)) continue;
    if (remCall && localCallIds.has(remCall)) continue;
    const insertAt = insertIndexForRemoteCard(withSlots, remote, remIdx);
    withSlots.splice(insertAt, 0, it);
    localIds.add(remId);
    if (remCall) localCallIds.add(remCall);
  }

  // Prefer-local keeps extra tool cards, but remote swarm_result / swarm_pending
  // rows remain authoritative by stable identity (job_id / normalized job ids).
  const remoteResultsById = new Map<string, SwarmResultItem>();
  for (const it of remote) {
    if (it.kind === "swarm_result" && it.job_id) {
      remoteResultsById.set(String(it.job_id), it);
    }
  }
  const withResults = withSlots.map((it) => {
    if (it.kind !== "swarm_result" || !it.job_id) return it;
    const rem = remoteResultsById.get(String(it.job_id));
    if (!rem) return it;
    return mergeSwarmResultReuse(it, rem);
  });
  const localResultIds = new Set(
    withResults
      .filter((it): it is SwarmResultItem => it.kind === "swarm_result" && !!it.job_id)
      .map((it) => String(it.job_id)),
  );
  for (const rem of remote) {
    if (rem.kind !== "swarm_result" || !rem.job_id) continue;
    const id = String(rem.job_id);
    if (localResultIds.has(id)) continue;
    withResults.push(rem);
    localResultIds.add(id);
  }

  type SwarmPendingRow = Extract<Item, { kind: "swarm_pending" }>;
  const remotePendingByKey = new Map<string, SwarmPendingRow>();
  for (const it of remote) {
    if (it.kind !== "swarm_pending") continue;
    const key = swarmPendingIdentityKey(it.job_ids);
    if (key) remotePendingByKey.set(key, it);
  }
  const withPending = withResults.map((it) => {
    if (it.kind !== "swarm_pending") return it;
    const key = swarmPendingIdentityKey(it.job_ids);
    if (!key) return it;
    const rem = remotePendingByKey.get(key);
    if (!rem) return it;
    return mergeSwarmPendingItems(it, rem);
  });
  const localPendingKeys = new Set<string>();
  for (const it of withPending) {
    if (it.kind !== "swarm_pending") continue;
    const key = swarmPendingIdentityKey(it.job_ids);
    if (key) localPendingKeys.add(key);
  }
  for (const rem of remote) {
    if (rem.kind !== "swarm_pending") continue;
    const key = swarmPendingIdentityKey(rem.job_ids);
    if (!key || localPendingKeys.has(key)) continue;
    withPending.push(rem);
    localPendingKeys.add(key);
  }

  // Harden against poll/SSE interleave leaving duplicate tool-call ids in local.
  return dedupeDisplayItems(appendMissingPendingApprovals(withPending, local));
}

/** Bound path/id lists so fingerprints stay cheap and order-stable. */
function fingerprintPathList(paths: readonly string[] | undefined, limit = 8): string {
  if (!Array.isArray(paths) || paths.length === 0) return "";
  return [...paths].map(String).sort().slice(0, limit).join(",");
}

/** Cheap content fingerprint so busy-poll refresh can skip identical payloads. */
export function transcriptFingerprint(items: Item[]): string {
  let fp = `n=${items.length}`;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind === "msg") {
      fp += `|m:${it.msg.role}:${it.msg.text.length}:${it.msg.streaming ? 1 : 0}`;
    } else if (it.kind === "card") {
      const r = it.card.result;
      fp += `|c:${it.card.id}:${it.card.running ? 1 : 0}:${r ? 1 : 0}`;
    } else if (it.kind === "swarm_result") {
      // Include every reuse/correction field merge+hydrate must paint so a
      // validation_fingerprint-only clear (or error/reason update) cannot be
      // skipped by busy-poll / reattach fingerprint equality.
      const paths = fingerprintPathList(it.invalidated_paths);
      const files = fingerprintPathList(it.files);
      const criteria = Array.isArray(it.acceptance_criteria)
        ? it.acceptance_criteria.join("\u001f")
        : "";
      fp += `|s:${it.job_id}:${it.applied ? 1 : 0}:${it.held_for_review ? 1 : 0}:${it.analysis_ok ? 1 : 0}:${it.reuse_status || ""}:${it.source_job_id || ""}:${it.reuse_reason || ""}:${it.validation_fingerprint || ""}:${it.environment_fingerprint || ""}:${it.error || ""}:${files}:${paths}:${criteria}`;
    } else if (it.kind === "pending_review") {
      fp += `|pr:${it.id}:${(it.summary || "").length}`;
    } else if (it.kind === "swarm_pending") {
      // Remote running→terminal (and terminal_job_ids union) must change the
      // fingerprint so prefer-local merge paints the settled pill.
      const status = swarmPendingStatusOf(it);
      const ids = normalizeSwarmJobIds(it.job_ids || []).slice(0, 16).join(",");
      const terminals = normalizeSwarmJobIds(it.terminal_job_ids || []).slice(0, 16).join(",");
      fp += `|sp:${ids}:${status}:${status === "running" ? 1 : 0}:${terminals}`;
    } else if (it.kind === "command_approval") {
      fp += `|ca:${it.commandHash}:${it.status}`;
    } else if (it.kind === "secret_request") {
      fp += `|sr:${it.connector}:${it.field}:${it.status}`;
    } else if (it.kind === "thinking") {
      fp += `|t:${(it.text || "").length}:${(it as { streaming?: boolean }).streaming ? 1 : 0}`;
    } else if (it.kind === "tool_prep") {
      fp += `|p:${String((it as { name?: string }).name || "")}`;
    } else if (it.kind === "turn_terminal") {
      fp += `|tt:${it.id || ""}:${it.cause}:${it.state}:${(it.text || "").length}`;
    } else {
      fp += `|o:${it.kind}`;
    }
  }
  return fp;
}
