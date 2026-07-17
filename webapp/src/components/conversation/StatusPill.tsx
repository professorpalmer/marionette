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

export default function StatusPill({ status, detail }: { status: string; detail?: string }) {
  const label = statusPillLabel(status, detail);
  return (
    <span
      className={`text-[10.5px] flex items-center gap-1.5 min-w-0 max-w-[42ch] ${statusPillTextClass(status)}`}
      title={detail || status}
    >
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusPillDotClass(status)}`} />
      <span className="truncate">{label}</span>
    </span>
  );
}
