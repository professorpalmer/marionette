import { useEffect, useRef, useState } from "react";
import { ChevronDown, CheckCircle2, Circle, Loader2, XCircle, ListChecks } from "lucide-react";
import { api, type Job } from "../lib/api";
import { usePolling } from "../lib/usePolling";

// Compact task strip (Cursor/Hermes composer-header pattern): a single slim line
// at the TOP of the chat column summarizing this session's PM jobs, with a
// click-to-expand popover listing them. Tasks = jobs this session spawned
// (swarm/implement/parallel runs); running ones surface first. Fed from
// /api/jobs. Collapsed by default so it stays out of the way -- the rich live
// view lives in the Swarm Tracker.

type Status = "pending" | "in_progress" | "completed" | "cancelled";

function jobStatus(j: Job): Status {
  const s = (j.status || "").toLowerCase();
  if (s.includes("complete") || s.includes("done")) return "completed";
  if (s.includes("fail") || s.includes("cancel") || s.includes("error")) return "cancelled";
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "in_progress";
  return "pending";
}

export default function TaskStack({ refresh }: { refresh: number }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // eslint-disable-next-line react-hooks/exhaustive-deps -- refresh forces a
  // fresh fetch via the identity of the returned promise chain below.
  usePolling(() => api.jobs().then(setJobs).catch(() => {}), 2500);
  useEffect(() => { api.jobs().then(setJobs).catch(() => {}); }, [refresh]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  if (jobs.length === 0) return null;

  const done = jobs.filter((j) => jobStatus(j) === "completed").length;
  const running = jobs.filter((j) => jobStatus(j) === "in_progress").length;
  // Running jobs first, then most-recent, so the active work reads at a glance.
  const ordered = jobs
    .slice()
    .reverse()
    .sort((a, b) => (jobStatus(b) === "in_progress" ? 1 : 0) - (jobStatus(a) === "in_progress" ? 1 : 0));

  return (
    <div ref={ref} className="relative shrink-0 border-b border-edge bg-panel2/40 select-none">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[11px] hover:bg-panel2/70 transition-colors focus:outline-none"
      >
        {running > 0 ? (
          <Loader2 size={12} className="animate-spin text-accent shrink-0" />
        ) : (
          <ListChecks size={12} className="text-faint shrink-0" />
        )}
        <span className="text-muted font-medium tabular-nums">
          Tasks {done}/{jobs.length}
        </span>
        {running > 0 && (
          <span className="text-accent tabular-nums">· {running} running</span>
        )}
        <span className="flex-1" />
        <ChevronDown
          size={12}
          className={`text-faint transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>

      {open && (
        <div className="absolute left-0 right-0 top-full z-30 bg-panel2 border-b border-edge shadow-lg max-h-64 overflow-y-auto">
          {ordered.map((j) => {
            const st = jobStatus(j);
            return (
              <div key={j.id} className="flex items-center gap-2 px-3 py-1.5 text-[11px] border-t border-edge/40 first:border-t-0">
                <StatusIcon status={st} />
                <span
                  className={`flex-1 truncate ${st === "completed" ? "text-muted" : st === "cancelled" ? "text-risk/80" : "text-txt"}`}
                  title={j.goal}
                >
                  {j.goal}
                </span>
                {j.adapter && (
                  <span className="text-[9px] text-faint lowercase shrink-0">{j.adapter}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function StatusIcon({ status }: { status: Status }) {
  if (status === "completed") return <CheckCircle2 size={12} className="text-good shrink-0" />;
  if (status === "in_progress") return <Loader2 size={12} className="animate-spin text-accent shrink-0" />;
  if (status === "cancelled") return <XCircle size={12} className="text-risk shrink-0" />;
  return <Circle size={12} className="text-muted shrink-0" />;
}
