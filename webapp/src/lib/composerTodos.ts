import type { Job, SessionTodoItem, SessionTodoPhase, SessionTodoSnapshot } from "./api";
import { taskState } from "./composerTasks";
import { jobInActiveSession } from "./jobScope";

export const TODO_OPEN_CAP = 5;
export const TODO_CLOSED_CONTEXT = 2;

const CLOSED = new Set(["completed", "abandoned"]);

export function todoPhaseProgress(phase: SessionTodoPhase): { done: number; total: number } {
  const total = phase.tasks.length;
  const done = phase.tasks.filter((task) => CLOSED.has(task.status)).length;
  return { done, total };
}

export function todoSnapshotProgress(snapshot: SessionTodoSnapshot | null | undefined): { done: number; total: number } {
  const phases = snapshot?.phases || [];
  return phases.reduce(
    (acc, phase) => {
      const { done, total } = todoPhaseProgress(phase);
      acc.done += done;
      acc.total += total;
      return acc;
    },
    { done: 0, total: 0 },
  );
}

export function collapseTodoTasks(
  tasks: readonly SessionTodoItem[],
  litContents?: ReadonlySet<string>,
): {
  items: SessionTodoItem[];
  hidden: number;
} {
  const closed = tasks.filter((task) => CLOSED.has(task.status));
  const open = tasks.filter((task) => !CLOSED.has(task.status));
  const lead = closed.slice(-TODO_CLOSED_CONTEXT);
  const lit = litContents
    ? open.filter((task) => litContents.has(task.content))
    : [];
  const rest = litContents
    ? open.filter((task) => !litContents.has(task.content))
    : open;
  const shownOpen = [...lit, ...rest].slice(0, TODO_OPEN_CAP);
  const items = [...lead, ...shownOpen];
  return { items, hidden: Math.max(0, tasks.length - items.length) };
}

export const TODO_DESCRIPTION_MIN_OVERLAP = 6;

const GENERIC_TODO_LABEL_TOKENS = new Set([
  "implement",
  "explore",
  "audit",
  "review",
  "architect",
  "worker",
  "swarm",
  "plan",
  "agentic",
  "analysis",
  "parallel",
  "wave",
  "coder",
]);

export function normalizeForTodoMatch(value: string): string {
  return (value || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

export function isGenericTodoLabel(value: string): boolean {
  const tokens = normalizeForTodoMatch(value).split(/\s+/).filter(Boolean);
  const substantive = tokens.filter((token) => token.length >= 3 || /\d/.test(token));
  if (!substantive.length) return true;
  return substantive.every((token) => GENERIC_TODO_LABEL_TOKENS.has(token));
}

function usableTodoLabel(value: string): boolean {
  const normalized = normalizeForTodoMatch(value);
  return normalized.length >= TODO_DESCRIPTION_MIN_OVERLAP && !isGenericTodoLabel(value);
}

function todoMatchScore(content: string, descriptions: readonly string[]): number {
  const target = normalizeForTodoMatch(content);
  if (!target) return 0;
  let best = 0;
  for (const desc of descriptions) {
    if (!usableTodoLabel(desc)) continue;
    const candidate = normalizeForTodoMatch(desc);
    if (target === candidate) return Math.max(best, target.length + 1_000);
    if (target.length >= TODO_DESCRIPTION_MIN_OVERLAP && candidate.includes(target)) {
      best = Math.max(best, target.length);
    }
    if (candidate.length >= TODO_DESCRIPTION_MIN_OVERLAP && target.includes(candidate)) {
      best = Math.max(best, candidate.length);
    }
  }
  return best;
}

export function todoMatchesAnyDescription(
  content: string,
  descriptions: readonly string[],
): boolean {
  return todoMatchScore(content, descriptions) > 0;
}

function openTodoItems(snapshot: SessionTodoSnapshot | null | undefined): SessionTodoItem[] {
  const open: SessionTodoItem[] = [];
  for (const phase of snapshot?.phases || []) {
    for (const task of phase.tasks) {
      if (task.status === "pending" || task.status === "in_progress") open.push(task);
    }
  }
  return open;
}

function bestMatchingOpenTodo(
  snapshot: SessionTodoSnapshot | null | undefined,
  descriptions: readonly string[],
): string | null {
  let winner: SessionTodoItem | null = null;
  let bestScore = 0;
  for (const task of openTodoItems(snapshot)) {
    const score = todoMatchScore(task.content, descriptions);
    if (score <= 0) continue;
    if (
      score > bestScore
      || (score === bestScore && task.status === "in_progress" && winner?.status !== "in_progress")
    ) {
      winner = task;
      bestScore = score;
    }
  }
  return winner?.content ?? null;
}

export function liveJobTodoLabelGroups(jobs: readonly Job[], sessionId: string): string[][] {
  const groups: string[][] = [];
  for (const job of jobs) {
    if (!jobInActiveSession(job, sessionId)) continue;
    const jobLive = taskState(job.status) === "in_progress";
    const runningTasks = (job.tasks || []).filter((task) => taskState(task.status) === "in_progress");
    if (!jobLive && !runningTasks.length) continue;
    if (runningTasks.length) {
      for (const task of runningTasks) {
        const labels: string[] = [];
        if (job.goal && usableTodoLabel(job.goal)) labels.push(job.goal);
        const instruction = String(task.instruction || "");
        if (usableTodoLabel(instruction)) labels.push(instruction);
        if (labels.length) groups.push(labels);
      }
      continue;
    }
    if (job.goal && usableTodoLabel(job.goal)) groups.push([job.goal]);
  }
  return groups;
}

export function liveJobTodoLabels(jobs: readonly Job[], sessionId: string): string[] {
  return liveJobTodoLabelGroups(jobs, sessionId).flat();
}

export function litTodoContents(
  snapshot: SessionTodoSnapshot | null | undefined,
  descriptions: readonly string[],
): Set<string> {
  return litTodoContentsFromGroups(snapshot, descriptions.length ? [descriptions] : []);
}

export function litTodoContentsFromGroups(
  snapshot: SessionTodoSnapshot | null | undefined,
  groups: readonly (readonly string[])[],
): Set<string> {
  const lit = new Set<string>();
  for (const group of groups) {
    const winner = bestMatchingOpenTodo(snapshot, group);
    if (winner) lit.add(winner);
  }
  return lit;
}

export function todoHasWork(snapshot: SessionTodoSnapshot | null | undefined): boolean {
  return (snapshot?.phases || []).some((phase) => phase.tasks.length > 0);
}

export function toRoman(value: number): string {
  const table: Array<[number, string]> = [
    [10, "X"],
    [9, "IX"],
    [5, "V"],
    [4, "IV"],
    [1, "I"],
  ];
  let remaining = Math.max(1, Math.floor(value));
  let out = "";
  for (const [arabic, glyph] of table) {
    while (remaining >= arabic) {
      out += glyph;
      remaining -= arabic;
    }
  }
  return out;
}
