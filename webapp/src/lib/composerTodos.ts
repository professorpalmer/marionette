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

export function normalizeForTodoMatch(value: string): string {
  return (value || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

export function todoMatchesAnyDescription(
  content: string,
  descriptions: readonly string[],
): boolean {
  const target = normalizeForTodoMatch(content);
  if (!target) return false;
  for (const desc of descriptions) {
    const candidate = normalizeForTodoMatch(desc);
    if (!candidate) continue;
    if (target === candidate) return true;
    if (target.length >= TODO_DESCRIPTION_MIN_OVERLAP && candidate.includes(target)) return true;
    if (candidate.length >= TODO_DESCRIPTION_MIN_OVERLAP && target.includes(candidate)) return true;
  }
  return false;
}

export const TODO_DESCRIPTION_MIN_OVERLAP = 6;

export function liveJobTodoLabels(jobs: readonly Job[], sessionId: string): string[] {
  const labels: string[] = [];
  for (const job of jobs) {
    if (!jobInActiveSession(job, sessionId)) continue;
    const jobLive = taskState(job.status) === "in_progress" || taskState(job.status) === "pending";
    const liveTasks = (job.tasks || []).filter((task) => {
      const state = taskState(task.status);
      return state === "in_progress" || state === "pending";
    });
    if (!jobLive && !liveTasks.length) continue;
    if (job.goal) labels.push(job.goal);
    if (job.role) labels.push(job.role);
    for (const task of liveTasks) {
      if (task.instruction) labels.push(String(task.instruction));
      if (task.role) labels.push(String(task.role));
    }
  }
  return labels;
}

export function litTodoContents(
  snapshot: SessionTodoSnapshot | null | undefined,
  descriptions: readonly string[],
): Set<string> {
  const lit = new Set<string>();
  for (const phase of snapshot?.phases || []) {
    for (const task of phase.tasks) {
      if (task.status !== "pending") continue;
      if (todoMatchesAnyDescription(task.content, descriptions)) {
        lit.add(task.content);
      }
    }
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
