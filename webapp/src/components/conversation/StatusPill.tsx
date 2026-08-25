const STATUS_TEXT: Record<string, string> = {
  idle: "text-faint",
  thinking: "text-accent",
  executing: "text-warn",
  investigating: "text-warn",
  streaming: "text-accent",
  awaiting_swarm: "text-warn",
  done: "text-good",
  error: "text-risk",
  "switching…": "text-accent",
};

const STATUS_DOT: Record<string, string> = {
  idle: "bg-faint",
  thinking: "bg-accent animate-pulse",
  executing: "bg-warn animate-pulse",
  investigating: "bg-warn animate-pulse",
  streaming: "bg-accent animate-pulse",
  awaiting_swarm: "bg-warn animate-pulse",
  done: "bg-good",
  error: "bg-risk",
  "switching…": "bg-accent animate-pulse",
};

const BUSY_PILL_STATUSES = new Set([
  "thinking",
  "executing",
  "investigating",
  "streaming",
  "awaiting_swarm",
]);

/** True when the pill may focus the live terminal via onDetailClick. */
export function statusPillClickable(
  status: string,
  detail: string | undefined,
  onDetailClick: (() => void) | undefined,
): boolean {
  if (!onDetailClick || !BUSY_PILL_STATUSES.has(status)) return false;
  // awaiting_swarm: clickable even without detail (Still working… is enough).
  if (status === "awaiting_swarm") return true;
  return Boolean(detail);
}

/** Visible label: prefer busy detail; never flash raw machine enums. */
export function statusPillLabel(status: string, detail?: string): string {
  if (detail && BUSY_PILL_STATUSES.has(status)) {
    return detail;
  }
  if (status === "awaiting_swarm") return "Still working…";
  if (status === "investigating" || status === "executing") return "Investigating…";
  if (status === "thinking" || status === "streaming") return "Still working…";
  if (status === "idle") return "Ready";
  if (status === "done") return "Done";
  if (status === "error") return "Error";
  return status;
}

/** Hover text: error keeps the compact label and discloses the safe reason. */
export function statusPillHoverText(status: string, detail?: string): string {
  if (status === "error" && detail) return detail;
  return detail || status;
}

export function statusPillTextClass(status: string): string {
  return STATUS_TEXT[status] || STATUS_TEXT.idle;
}

export function statusPillDotClass(status: string): string {
  return STATUS_DOT[status] || STATUS_DOT.idle;
}

export default function StatusPill({
  status,
  detail,
  onDetailClick,
}: {
  status: string;
  detail?: string;
  /** When set, the busy detail (e.g. "Investigating…") focuses the live surface. */
  onDetailClick?: () => void;
}) {
  const label = statusPillLabel(status, detail);
  const hoverText = statusPillHoverText(status, detail);
  const clickable = statusPillClickable(status, detail, onDetailClick);
  const className =
    `text-[10.5px] font-normal flex items-center gap-1.5 min-w-0 max-w-[42ch] ${statusPillTextClass(status)}`
    + (clickable ? " cursor-pointer hover:underline underline-offset-2" : "");
  if (clickable) {
    return (
      <button
        type="button"
        onClick={onDetailClick}
        className={className}
        title="Open terminal for live worker output"
      >
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusPillDotClass(status)}`} />
        <span className="truncate">{label}</span>
      </button>
    );
  }
  return (
    <span
      className={className}
      title={hoverText}
    >
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusPillDotClass(status)}`} />
      <span className="truncate">{label}</span>
    </span>
  );
}
