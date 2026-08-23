import type { CSSProperties } from "react";
import StatusPill from "./StatusPill";
import {
  TITLEBAR_CHROME_PAD_X_PX,
  TITLEBAR_TRAFFIC_PAD_PX,
} from "../../lib/titlebarSafe";

/** Brand strip + status pill for the conversation pane. */
export default function ConversationHeader({
  pillStatus,
  detail,
  onBusyDetailClick,
  recoveryAction,
}: {
  pillStatus: string;
  detail?: string;
  onBusyDetailClick?: () => void;
  recoveryAction?: { label: string; onClick: () => void };
}) {
  const dragRegion = { WebkitAppRegion: "drag" } as CSSProperties;
  const noDrag = { WebkitAppRegion: "no-drag" } as CSSProperties;
  return (
    // With no update / provider-key banner above it and the left rail collapsed,
    // this header is the topmost row at x=0, so it must clear the macOS
    // hiddenInset traffic lights itself -- the chrome pad alone lands under them.
    <header
      data-testid="conversation-header"
      className="flex items-center justify-between border-b border-edge/60 shrink-0"
      style={{
        paddingTop: 8,
        paddingBottom: 7,
        paddingLeft: TITLEBAR_TRAFFIC_PAD_PX,
        paddingRight: TITLEBAR_CHROME_PAD_X_PX,
        ...dragRegion,
      }}
    >
      <span className="flex items-baseline gap-1.5 select-none min-w-0" style={noDrag}>
        <span className="font-semibold text-[12px] text-txt/90 tracking-tight">Marionette</span>
        <span className="text-faint/70 text-[9px] font-normal">|</span>
        <span className="text-muted/80 text-[9px] font-medium tracking-wide uppercase truncate">
          The Puppetmaster Harness
        </span>
      </span>
      <div className="shrink-0 flex items-center gap-2" style={noDrag}>
        {recoveryAction ? (
          <button
            type="button"
            onClick={recoveryAction.onClick}
            className="text-[10px] px-2 py-0.5 rounded-md border border-edge/60 bg-panel2/60 text-muted hover:text-txt transition"
          >
            {recoveryAction.label}
          </button>
        ) : null}
        <StatusPill
          status={pillStatus}
          detail={detail}
          onDetailClick={onBusyDetailClick}
        />
      </div>
    </header>
  );
}
