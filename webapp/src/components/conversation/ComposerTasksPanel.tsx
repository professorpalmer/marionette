import { useState } from "react";
import { CheckCircle2, ChevronDown, ChevronRight, Circle, Loader2, ListChecks, XCircle } from "lucide-react";
import type { Job } from "../../lib/api";
import { buildComposerTasks, pickTaskSourceJob, taskProgress, type ComposerTask } from "../../lib/composerTasks";

function TaskIcon({ state }: { state: ComposerTask["state"] }) {
  if (state === "completed") return <CheckCircle2 size={13} className="shrink-0 text-good" />;
  if (state === "failed") return <XCircle size={13} className="shrink-0 text-risk" />;
  if (state === "in_progress") return <Loader2 size={13} className="shrink-0 animate-spin text-accent" />;
  return <Circle size={13} className="shrink-0 text-faint" />;
}

export default function ComposerTasksPanel({
  jobs,
  sessionId,
}: {
  jobs: readonly Job[];
  sessionId: string;
}) {
  const [open, setOpen] = useState(false);
  const job = pickTaskSourceJob(jobs, sessionId);
  const tasks = buildComposerTasks(job);
  const { done, total } = taskProgress(tasks);
  if (!total) return null;

  return (
    <div className="mx-2 mb-2 overflow-hidden rounded-2xl border border-edge/80 bg-panel2/80 shadow-lg shadow-black/15">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12px] text-txt/90 hover:bg-panel/40"
      >
        {open ? <ChevronDown size={13} className="text-faint" /> : <ChevronRight size={13} className="text-faint" />}
        <ListChecks size={13} className="text-faint" />
        <span className="font-medium">Tasks {done}/{total}</span>
      </button>
      {open && (
        <div className="border-t border-edge/60 px-2.5 py-1.5 space-y-1">
          {tasks.map((task) => (
            <div key={task.id} className="flex items-start gap-2 py-0.5 text-[12px] leading-4">
              <TaskIcon state={task.state} />
              <span className={task.state === "pending" ? "text-faint" : "text-txt/90"}>{task.content}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
