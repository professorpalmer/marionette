import type { CSSProperties } from "react";
import StatusPill from "./StatusPill";

/** Brand strip + status pill for the conversation pane. */
export default function ConversationHeader({
  pillStatus,
  detail,
  onBusyDetailClick,
}: {
  pillStatus: string;
  detail?: string;
  onBusyDetailClick?: () => void;
}) {
  const dragRegion = { WebkitAppRegion: "drag" } as CSSProperties;
  const noDrag = { WebkitAppRegion: "no-drag" } as CSSProperties;
  return (
    <header
      className="flex items-center justify-between border-b border-edge/60 shrink-0 px-6"
      style={{ paddingTop: 8, paddingBottom: 7, ...dragRegion }}
    >
      <span className="flex items-baseline gap-1.5 select-none min-w-0" style={noDrag}>
        <span className="font-semibold text-[12px] text-txt/90 tracking-tight">Marionette</span>
        <span className="text-faint/70 text-[9px] font-normal">|</span>
        <span className="text-muted/80 text-[9px] font-medium tracking-wide uppercase truncate">
          The Puppetmaster Harness
        </span>
      </span>
      <div className="shrink-0" style={noDrag}>
        <StatusPill
          status={pillStatus}
          detail={detail}
          onDetailClick={onBusyDetailClick}
        />
      </div>
    </header>
  );
}
