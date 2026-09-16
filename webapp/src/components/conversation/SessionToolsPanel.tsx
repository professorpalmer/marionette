import { useRef } from "react";
import { OverlayPortal } from "../../lib/overlayPortal";
import AdvicePane from "../AdvicePane";
import SchedulesPane from "../SchedulesPane";
import { scheduleButton } from "../ScheduleEditor";

export type SessionToolsView = { kind: "advice" | "routines"; sessionId: string; error: string };

export default function SessionToolsPanel({ view, onClose }: { view: SessionToolsView | null; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  return <OverlayPortal open={view !== null} onClose={onClose} focusRootRef={panel} initialFocusRef={close}
    onBackdropClick={onClose} className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
    {view && <div ref={panel} role="dialog" aria-label={view.kind === "advice" ? "Advice" : "Routines"}
      className="w-[min(920px,92vw)] max-h-[85vh] overflow-auto rounded border border-edge bg-panel p-4" onClick={e => e.stopPropagation()}>
      <div className="mb-3 flex items-center justify-between"><h2 className="text-txt">{view.kind === "advice" ? "Advice" : "Routines"}</h2><button ref={close} type="button" className={scheduleButton} onClick={onClose}>Close</button></div>
      {view.error && <p role="alert" className="text-risk">{view.error}</p>}
      {view.kind === "advice" ? <AdvicePane key={view.sessionId} sessionId={view.sessionId} /> : <SchedulesPane />}
    </div>}
  </OverlayPortal>;
}
