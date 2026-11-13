import { useState, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

/**
 * One divider row for a system event in the transcript (context compacted,
 * model switched). A rule on each side, an icon, a label, an optional
 * detail, and an optional toggle that opens more below the row.
 */
export function SystemEventRow({
  icon: Icon,
  label,
  detail,
  title,
  tone = "quiet",
  moreLabel,
  more,
  testId,
}: {
  icon: LucideIcon;
  label: string;
  detail?: string;
  title?: string;
  tone?: "quiet" | "warn";
  /** Text of the toggle that shows `more` ("View summary"). */
  moreLabel?: string;
  more?: ReactNode;
  testId?: string;
}) {
  const [open, setOpen] = useState(false);
  const toneClass = tone === "warn" ? "text-warn/90" : "text-faint";
  return (
    <div className="my-2 flex flex-col gap-1" data-testid={testId ?? "system-event"}>
      <div role="status" title={title} className={`flex items-center gap-2 text-ui-11 select-none ${toneClass}`}>
        <span aria-hidden className="h-px flex-1 bg-edge/40" />
        <Icon size={12} aria-hidden className="shrink-0" />
        <span className="min-w-0 truncate">
          {label}
          {detail ? <span className="text-faint/80"> · {detail}</span> : null}
        </span>
        {more && moreLabel ? (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="shrink-0 bg-transparent border-0 p-0 text-ui-11 text-muted hover:text-txt underline-offset-2 hover:underline cursor-pointer transition-colors"
          >
            {open ? "Hide summary" : moreLabel}
          </button>
        ) : null}
        <span aria-hidden className="h-px flex-1 bg-edge/40" />
      </div>
      {open && more ? <div className="mx-auto w-full max-w-[85%] text-ui-11 text-faint">{more}</div> : null}
    </div>
  );
}

/** "14.3k" for a token count; under 1000 stays exact. */
export function compactTokens(n: number): string {
  if (!Number.isFinite(n)) return "";
  if (Math.abs(n) < 1000) return String(Math.round(n));
  const k = n / 1000;
  return `${k >= 100 ? Math.round(k) : Math.round(k * 10) / 10}k`;
}
