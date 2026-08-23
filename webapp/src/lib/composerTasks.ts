import type { Job, Task } from "./api";
import { jobInActiveSession } from "./jobScope";

export type ComposerTaskState = "pending" | "in_progress" | "completed" | "failed";

export type ComposerTask = {
  id: string;
  content: string;
  state: ComposerTaskState;
};

export function taskState(status: unknown): ComposerTaskState {
  const s = String(status || "").trim().toLowerCase();
  if (s.includes("fail") || s.includes("error") || s.includes("dead")) return "failed";
  if (s.includes("complete") || s.includes("done") || s === "ok" || s.includes("success")) return "completed";
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "in_progress";
  return "pending";
}

function jobRank(job: Job): number {
  const st = taskState(job.status);
  if (st === "in_progress") return 0;
  if (st === "failed") return 1;
  if (st === "completed") return 2;
  return 3;
}

export function pickTaskSourceJob(jobs: readonly Job[], activeSessionId: string): Job | null {
  const scoped = jobs.filter((job) => jobInActiveSession(job, activeSessionId) && (job.tasks || []).length);
  if (!scoped.length) return null;
  return [...scoped].sort((a, b) => {
    const rank = jobRank(a) - jobRank(b);
    if (rank !== 0) return rank;
    return String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""));
  })[0];
}

function oneLineLabel(task: Task): string {
  const role = String(task.role || "").replace(/\s+/g, " ").trim();
  const instruction = String(task.instruction || "").replace(/\s+/g, " ").trim();
  if (role && instruction && !instruction.toLowerCase().startsWith(role.toLowerCase())) {
    return `${role} · ${instruction}`;
  }
  return instruction || role || String(task.id || "Task");
}

export function buildComposerTasks(job: Job | null): ComposerTask[] {
  return (job?.tasks || []).map((task: Task, i) => ({
    id: String(task.id || `${job?.id || "job"}-${i}`),
    content: oneLineLabel(task),
    state: taskState(task.status),
  }));
}

export function taskProgress(tasks: readonly ComposerTask[]): { done: number; total: number } {
  return {
    done: tasks.filter((t) => t.state === "completed").length,
    total: tasks.length,
  };
}
