import { useRef } from "react";
import { Modal } from "../ui/overlay";
import AdvicePane from "../AdvicePane";
import SchedulesPane from "../SchedulesPane";
import { scheduleButton } from "../ScheduleEditor";

export type SessionToolsView = { kind: "advice" | "routines"; sessionId: string; error: string };

export default function SessionToolsPanel({ view, onClose }: { view: SessionToolsView | null; onClose: () => void }) {
  const close = useRef<HTMLButtonElement>(null);
  const title = view?.kind === "routines" ? "Routines" : "Advice";
  return <Modal open={view !== null} onClose={onClose} label={title} initialFocus={close}
    backdropClassName="fixed inset-0 z-50 bg-black/70"
    viewportClassName="fixed inset-0 z-50 flex items-center justify-center"
    className="w-[min(920px,92vw)] max-h-[85vh] overflow-auto rounded border border-edge bg-panel p-4 outline-none">
    {view && <>
      <div className="mb-3 flex items-center justify-between"><h2 className="text-txt">{title}</h2><button ref={close} type="button" className={scheduleButton} onClick={onClose}>Close</button></div>
      {view.error && <p role="alert" className="text-risk">{view.error}</p>}
      {view.kind === "advice" ? <AdvicePane key={view.sessionId} sessionId={view.sessionId} /> : <SchedulesPane />}
    </>}
  </Modal>;
}
