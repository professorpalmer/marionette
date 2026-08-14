import type { ReactNode } from "react";
import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";

export type JobStatus = "pending" | "in_progress" | "completed" | "cancelled";

export function JobStatusIcon({ status }: { status: JobStatus }) {
  if (status === "completed") return <CheckCircle2 size={12} className="text-good shrink-0" />;
  if (status === "in_progress") return <Loader2 size={12} className="animate-spin text-accent shrink-0" />;
  if (status === "cancelled") return <XCircle size={12} className="text-red-400 shrink-0" />;
  return <Circle size={12} className="text-muted shrink-0" />;
}

/** Compact running/idle indicator for a session row. Hidden when status unknown.
 *  When stoppable (running + non-active), click Stops that runner without view attach. */
export function RunnerStatusDot({
  status,
  stoppable,
  onStop,
}: {
  status?: "running" | "idle" | "attaching" | "missing";
  stoppable?: boolean;
  onStop?: () => void;
}) {
  if (!status || status === "missing") return null;
  const running = status === "running";
  if (stoppable && running && onStop) {
    return (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          onStop();
        }}
        className="w-1.5 h-1.5 rounded-full shrink-0 bg-accent hover:ring-2 hover:ring-accent/40 transition"
        title="Stop (free lease slot)"
        aria-label="Stop background session"
      />
    );
  }
  const title =
    status === "attaching" ? "Attaching" : running ? "Running" : "Idle";
  return (
    <span
      className={`w-1.5 h-1.5 rounded-full shrink-0 ${running ? "bg-accent" : "bg-muted/50"}`}
      title={title}
    />
  );
}

export function Section({ title, action, headerSpinner, children, className }: {
  title: string;
  action?: ReactNode;
  headerSpinner?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`px-2 shrink-0 min-w-0 ${className || "pt-3"}`}>
      <div className="flex items-center justify-between px-1.5 mb-1.5 mt-0.5">
        <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.12em] text-faint font-semibold">
          {title}
          {headerSpinner && <Loader2 size={10} className="animate-spin text-muted shrink-0" />}
        </span>
        {action}
      </div>
      {children}
    </div>
  );
}
export const IconBtn = ({ onClick, children, title, disabled }: {
  onClick?: () => void;
  children?: ReactNode;
  title?: string;
  disabled?: boolean;
}) => (
  <button
    onClick={onClick}
    title={title}
    disabled={disabled}
    className="text-faint hover:text-txt p-1 rounded hover:bg-panel2/60 disabled:opacity-50 disabled:pointer-events-none"
  >
    {children}
  </button>
);
export const Empty = ({ children }: any) => <div className="text-[11px] text-faint italic px-1.5 py-1">{children}</div>;
