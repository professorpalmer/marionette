const STATUS_TEXT: Record<string, string> = {
  idle: "text-faint",
  thinking: "text-accent",
  executing: "text-warn",
  streaming: "text-accent",
  done: "text-good",
  error: "text-risk",
  "switching…": "text-accent",
};

const STATUS_DOT: Record<string, string> = {
  idle: "bg-faint",
  thinking: "bg-accent animate-pulse",
  executing: "bg-warn animate-pulse",
  streaming: "bg-accent animate-pulse",
  done: "bg-good",
  error: "bg-risk",
  "switching…": "bg-accent animate-pulse",
};

/** Visible label: prefer busy detail while thinking/executing/streaming. */
export function statusPillLabel(status: string, detail?: string): string {
  if (detail && (status === "thinking" || status === "executing" || status === "streaming")) {
    return detail;
  }
  return status;
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
  /** When set, the busy detail (e.g. "run implement") focuses the live surface. */
  onDetailClick?: () => void;
}) {
  const label = statusPillLabel(status, detail);
  const clickable =
    Boolean(onDetailClick)
    && Boolean(detail)
    && (status === "thinking" || status === "executing" || status === "streaming");
  const className =
    `text-[10.5px] flex items-center gap-1.5 min-w-0 max-w-[42ch] ${statusPillTextClass(status)}`
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
      title={detail || status}
    >
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusPillDotClass(status)}`} />
      <span className="truncate">{label}</span>
    </span>
  );
}
