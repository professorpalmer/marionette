import { useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { GitBranch } from "lucide-react";
import StatusPill from "./StatusPill";
import TraceCopy from "./TraceCopy";
import type { SessionHeading } from "./useSessionHeading";
import {
  TITLEBAR_CHROME_PAD_X_PX,
  TITLEBAR_TRAFFIC_PAD_PX,
  titlebarLeftPad,
} from "../../lib/titlebarSafe";

/** Left pad that clears the traffic lights only where this strip meets them;
 *  re-measured whenever the strip or the window changes size. */
function useTitlebarLeftPad(ref: { current: HTMLElement | null }): number {
  const [pad, setPad] = useState(TITLEBAR_TRAFFIC_PAD_PX);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setPad(titlebarLeftPad(el.getBoundingClientRect()));
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [ref]);
  return pad;
}

/**
 * Session title, project, and branch for the conversation pane. The turn
 * state is not here: the transcript live line owns it and the status bar
 * shows its dot. Only an error keeps a pill here, with its trace id.
 */
const NO_HEADING: SessionHeading = { title: "", project: "", branch: "" };

export default function ConversationHeader({
  heading = NO_HEADING,
  pillStatus,
  detail,
  correlationId,
  recoveryAction,
}: {
  heading?: SessionHeading;
  pillStatus: string;
  detail?: string;
  correlationId?: string;
  recoveryAction?: { label: string; onClick: () => void };
}) {
  const headerRef = useRef<HTMLElement>(null);
  const leftPad = useTitlebarLeftPad(headerRef);
  const dragRegion = { WebkitAppRegion: "drag" } as CSSProperties;
  const noDrag = { WebkitAppRegion: "no-drag" } as CSSProperties;
  return (
    // With no update / provider-key banner above it and the left rail collapsed,
    // this header is the topmost row at x=0 and must clear the macOS hiddenInset
    // traffic lights itself; beside an open rail it only needs the chrome pad.
    <header
      ref={headerRef}
      data-testid="conversation-header"
      data-turn-status={pillStatus}
      className="flex items-center gap-2 border-b border-edge/60 shrink-0 min-w-0 overflow-hidden"
      style={{
        paddingTop: 8,
        paddingBottom: 7,
        paddingLeft: leftPad,
        paddingRight: TITLEBAR_CHROME_PAD_X_PX,
        ...dragRegion,
      }}
    >
      <span className="flex items-baseline gap-2 select-none flex-1 min-w-0 overflow-hidden" style={noDrag}>
        <span className="font-semibold text-ui-12 text-txt/90 tracking-tight truncate min-w-0" data-testid="header-session-title" title={heading.title || undefined}>
          {heading.title || "Marionette"}
        </span>
        {heading.project ? (
          <span className="text-muted/80 text-ui-11 truncate shrink min-w-0 hidden min-[420px]:inline" data-testid="header-project">
            {heading.project}
          </span>
        ) : null}
        {heading.branch ? (
          <span className="text-faint text-ui-11 truncate shrink min-w-0 hidden min-[520px]:inline-flex items-center gap-1 self-center" data-testid="header-branch">
            <GitBranch size={10} aria-hidden className="shrink-0" />
            {heading.branch}
          </span>
        ) : null}
      </span>
      <div className="shrink-0 flex items-center gap-2 max-w-[55%] min-w-0" style={noDrag}>
        {recoveryAction ? (
          <button
            type="button"
            onClick={recoveryAction.onClick}
            className="text-ui-10 px-2 py-0.5 rounded-md border border-edge/60 bg-panel2/60 text-muted hover:text-txt transition"
          >
            {recoveryAction.label}
          </button>
        ) : null}
        {pillStatus === "error" && correlationId ? (
          <TraceCopy correlationId={correlationId} />
        ) : null}
        {pillStatus === "error" ? <StatusPill status={pillStatus} detail={detail} /> : null}
      </div>
    </header>
  );
}
