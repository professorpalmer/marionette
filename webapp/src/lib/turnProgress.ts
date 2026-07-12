/**
 * Live busy-turn progress for the transcript footer and header pill.
 *
 * A long diagnose used to sit on "running..." while tools burned tokens
 * invisibly. These helpers derive a scannable label from the same cards the
 * activity fold already knows about -- pure, so vitest can pin the contract.
 */

export type BusyStatus = "idle" | "thinking" | "executing" | "done" | "error" | "streaming" | string;

export type TurnCard = {
  id: string;
  goal: string;
  kind: string;
  running: boolean;
};

export type TurnItem =
  | { kind: "msg"; msg: { role: string; text: string; streaming?: boolean } }
  | { kind: "card"; card: TurnCard }
  | { kind: string; [key: string]: unknown };

export type BusyProgress = {
  /** Short phase word: thinking / running / streaming */
  phase: string;
  /** Full scannable line for the transcript footer */
  label: string;
  /** Compact label for the header StatusPill */
  pill: string;
  step: number;
  runningGoal: string;
  runningKind: string;
};

/** Items after the last user message (current turn), or all if none. */
export function itemsInCurrentTurn(items: TurnItem[]): TurnItem[] {
  let lastUser = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && (it as { msg: { role: string } }).msg.role === "user") {
      lastUser = i;
      break;
    }
  }
  return lastUser >= 0 ? items.slice(lastUser + 1) : items;
}

function cardsInTurn(items: TurnItem[]): TurnCard[] {
  const out: TurnCard[] = [];
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "card" && (it as { card: TurnCard }).card) {
      out.push((it as { card: TurnCard }).card);
    }
  }
  return out;
}

/** Prefer basename-ish tail of a path/goal so the pill stays readable. */
export function shortenGoal(goal: string, max = 42): string {
  const g = (goal || "").trim().replace(/\s+/g, " ");
  if (!g) return "";
  const parts = g.split(/[/\\]/);
  const tail = parts[parts.length - 1] || g;
  if (tail.length <= max) return tail;
  return tail.slice(0, max - 1) + "…";
}

export function formatBusyElapsed(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const rem = sec % 60;
  if (min < 60) return rem ? `${min}m ${rem}s` : `${min}m`;
  const hr = Math.floor(min / 60);
  const mRem = min % 60;
  return mRem ? `${hr}h ${mRem}m` : `${hr}h`;
}

/**
 * Derive the live busy line from transcript cards + stream status.
 * When idle/done/error, returns empty labels (caller hides the row).
 */
export function deriveBusyProgress(
  items: TurnItem[],
  status: BusyStatus,
  elapsedMs?: number | null,
): BusyProgress {
  const busy =
    status === "thinking" || status === "executing" || status === "streaming";
  const cards = cardsInTurn(items);
  const step = cards.length;
  const running = [...cards].reverse().find((c) => c.running);
  const runningKind = (running?.kind || "").replace(/_/g, " ").trim();
  const runningGoal = shortenGoal(running?.goal || "");

  let phase = "idle";
  if (status === "thinking") phase = "thinking";
  else if (status === "streaming") phase = "streaming";
  else if (status === "executing" || running) phase = "running";
  else if (busy) phase = "thinking";

  const elapsed =
    busy && elapsedMs != null && elapsedMs >= 1000
      ? formatBusyElapsed(elapsedMs)
      : "";

  if (!busy) {
    return {
      phase,
      label: "",
      pill: String(status || "idle"),
      step,
      runningGoal,
      runningKind,
    };
  }

  const parts: string[] = [];
  if (status === "streaming") parts.push("streaming");
  else if (running || status === "executing") parts.push("running");
  else parts.push("thinking");

  if (runningKind) parts.push(runningKind);
  else if (runningGoal) parts.push(runningGoal);
  if (step > 0) parts.push(`step ${step}`);
  if (elapsed) parts.push(elapsed);

  // Avoid "running · goal · step N" doubling the goal when kind is empty
  // but we already pushed goal -- handled above (kind preferred).
  const label = parts.join(" · ");

  const pillParts: string[] = [phase];
  if (runningKind) pillParts.push(runningKind);
  if (step > 0) pillParts.push(`${step}`);
  if (elapsed) pillParts.push(elapsed);

  return {
    phase,
    label,
    pill: pillParts.join(" · "),
    step,
    runningGoal,
    runningKind,
  };
}

/**
 * One-line summary for an open Investigating header while a tool is mid-flight.
 */
export function investigatingHeadline(
  actionCount: number,
  anyRunning: boolean,
  runningKind: string,
  runningGoal: string,
  kindSummary: string,
): string {
  if (actionCount <= 0) return "";
  if (anyRunning) {
    const focus = runningKind
      ? runningGoal
        ? `${runningKind} ${runningGoal}`
        : runningKind
      : runningGoal || "tool";
    return `step ${actionCount} · ${focus}`;
  }
  return `${actionCount} step${actionCount === 1 ? "" : "s"}${kindSummary ? ` -- ${kindSummary}` : ""}`;
}
