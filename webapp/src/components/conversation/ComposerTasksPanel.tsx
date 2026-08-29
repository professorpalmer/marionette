import { useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Circle, ListChecks, Loader2, XCircle } from "lucide-react";
import type { Job } from "../../lib/api";
import { isWaveCoordinator } from "../../lib/jobClassification";
import { buildComposerTasks, pickTaskSourceJob, taskProgress, waveHeaderText, type ComposerTask } from "../../lib/composerTasks";
import { COMPOSER_FAMILY_SECTION } from "./composerFamily";

function waveHeaderTone(status: string): string {
  const s = status.toLowerCase();
  if (s.includes("fail") || s.includes("timed")) return "text-risk";
  if (s === "partial") return "text-warn";
  if (s.includes("complete")) return "text-good";
  return "text-txt";
}

function TaskIcon({ state }: { state: ComposerTask["state"] }) {
  if (state === "completed") return <CheckCircle2 size={11} className="shrink-0 text-good" />;
  if (state === "degraded") return <AlertTriangle size={11} className="shrink-0 text-warn" />;
  if (state === "failed") return <XCircle size={11} className="shrink-0 text-risk" />;
  if (state === "in_progress") return <Loader2 size={11} className="shrink-0 animate-spin text-accent" />;
  return <Circle size={11} className="shrink-0 text-faint" />;
}

export default function ComposerTasksPanel({
  jobs,
  sessionId,
}: {
  jobs: readonly Job[];
  sessionId: string;
}) {
  const job = pickTaskSourceJob(jobs, sessionId);
  const tasks = job ? buildComposerTasks(job) : [];
  const live = tasks.some((task) => task.state === "in_progress");
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  if (!job) return null;
  const { done, total } = taskProgress(tasks);
  const wave = isWaveCoordinator(job);
  const header = wave ? waveHeaderText(job) : `Tasks ${done}/${total}`;
  const headerTone = wave ? waveHeaderTone(String(job.status || "")) : "text-txt";
  const open = userOpen ?? live;

  return (
    <div
      className={COMPOSER_FAMILY_SECTION}
      data-slot="composer-tasks-panel"
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setUserOpen(!open)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left hover:bg-panel/25 focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-1 focus-visible:outline-accent"
      >
        {open ? <ChevronDown size={11} className="shrink-0 text-faint" /> : <ChevronRight size={11} className="shrink-0 text-faint" />}
        <ListChecks size={11} className="shrink-0 text-faint" />
        <span className={`text-[10.5px] font-medium leading-4 ${headerTone}`}>{header}</span>
      </button>
      {open && (
        <div className="space-y-0.5 px-2 pb-1.5">
          {tasks.map((task) => {
            const expanded = expandedId === task.id;
            const canExpand = Boolean(task.detail || task.summary);
            return (
              <button
                key={task.id}
                type="button"
                aria-expanded={canExpand ? expanded : undefined}
                onClick={() => {
                  if (!canExpand) return;
                  setExpandedId((id) => (id === task.id ? null : task.id));
                }}
                className="flex w-full items-start gap-1.5 rounded-md px-1.5 py-1 text-left hover:bg-panel/25 focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-1 focus-visible:outline-accent"
              >
                <span className="mt-0.5">
                  <TaskIcon state={task.state} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className={`block text-[10.5px] leading-4 ${task.state === "pending" ? "text-faint" : "text-txt"}`}>
                    {task.content}
                  </span>
                  {!expanded && task.summary ? (
                    <span className="block truncate text-[10.5px] leading-4 text-faint">{task.summary}</span>
                  ) : null}
                  {expanded && (task.detail || task.summary) ? (
                    <span className="mt-0.5 block whitespace-pre-wrap break-words text-[10.5px] leading-4 text-faint">
                      {task.detail || task.summary}
                    </span>
                  ) : null}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
