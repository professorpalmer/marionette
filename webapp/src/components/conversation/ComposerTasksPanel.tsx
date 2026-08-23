import { useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Circle, Loader2, ListChecks, XCircle } from "lucide-react";
import type { Job } from "../../lib/api";
import { buildComposerTasks, pickTaskSourceJob, taskProgress, type ComposerTask } from "../../lib/composerTasks";

function TaskIcon({ state }: { state: ComposerTask["state"] }) {
  if (state === "completed") return <CheckCircle2 size={12} className="shrink-0 text-good" />;
  if (state === "failed") return <XCircle size={12} className="shrink-0 text-risk" />;
  if (state === "in_progress") return <Loader2 size={12} className="shrink-0 animate-spin text-accent" />;
  return <Circle size={12} className="shrink-0 text-faint" />;
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
  const tasks = buildComposerTasks(job);
  const { done, total } = taskProgress(tasks);
  if (!total) return null;

  return (
    <div className="mx-2 mb-1 overflow-hidden rounded-lg border border-edge/70 bg-panel2/70">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-[11px] leading-4 text-txt/85 hover:bg-panel/35"
      >
        {open ? <ChevronDown size={11} className="text-faint" /> : <ChevronRight size={11} className="text-faint" />}
        <ListChecks size={11} className="text-faint" />
        <span className="font-medium">Tasks {done}/{total}</span>
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
                className="flex w-full items-start gap-1.5 rounded-md px-0.5 py-0.5 text-left text-[11px] leading-4 hover:bg-panel/30"
              >
                <TaskIcon state={task.state} />
                <span className={`${task.state === "pending" ? "text-faint" : "text-txt/85"} ${expanded ? "whitespace-pre-wrap break-words" : "min-w-0 truncate"}`}>
                  {task.content}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
