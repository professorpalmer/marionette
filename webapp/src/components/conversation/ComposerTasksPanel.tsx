import { useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Circle, Loader2, ListChecks, XCircle } from "lucide-react";
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
  const [open, setOpen] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const job = pickTaskSourceJob(jobs, sessionId);
  if (!job) return null;
  const tasks = buildComposerTasks(job);
  const { done, total } = taskProgress(tasks);
  const wave = isWaveCoordinator(job);
  const header = wave ? waveHeaderText(job) : `Tasks ${done}/${total}`;
  const headerTone = wave ? waveHeaderTone(String(job.status || "")) : "text-txt";

  return (
    <div
      className={COMPOSER_FAMILY_SECTION}
      data-slot="composer-tasks-panel"
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-[10.5px] leading-4 text-txt hover:bg-panel/35"
      >
        {open ? <ChevronDown size={11} className="text-faint" /> : <ChevronRight size={11} className="text-faint" />}
        <ListChecks size={11} className="text-faint" />
        <span className={`font-medium ${headerTone}`}>{header}</span>
      </button>
      {open && (
        <div className="border-t border-edge/50 px-2 py-1 space-y-0.5">
          {tasks.map((task) => {
            const expanded = expandedId === task.id;
            return (
              <button
                key={task.id}
                type="button"
                title={task.content}
                onClick={() => setExpandedId((id) => (id === task.id ? null : task.id))}
                className="flex w-full items-start gap-1.5 rounded-md px-0.5 py-0.5 text-left text-[10.5px] leading-4 hover:bg-panel/30"
              >
                <TaskIcon state={task.state} />
                <span className={`${task.state === "pending" ? "text-faint" : "text-txt"} ${expanded ? "whitespace-pre-wrap break-words" : "min-w-0 truncate"}`}>
                  {task.content}
                  {expanded && task.detail ? (
                    <span className="mt-0.5 block text-faint">{task.detail}</span>
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
