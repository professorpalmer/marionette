import { useEffect, useId, useRef, useState } from "react";
import { api, type Session, type SessionForkPreview } from "../lib/api";
import { Modal } from "./ui/overlay";

type ForkState =
  | { kind: "closed" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; preview: SessionForkPreview; eventId: number; requestId: string; error?: string }
  | { kind: "creating"; preview: SessionForkPreview; eventId: number; requestId: string }
  | { kind: "created"; child: Session };

const buttonClass = "min-h-8 px-2 rounded text-sm text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-accent disabled:opacity-50";

/** Mount keyed by session.id, outside any session-row button. */
export function SessionFork({ session, sessions, onSelect, onCreated, open, onClose, returnFocus }: {
  returnFocus?: HTMLElement | null;
  open?: boolean;
  onClose?: () => void;
  session: Pick<Session, "id" | "title" | "forked_from">;
  sessions: Session[];
  onSelect: (id: string) => void;
  onCreated: (child: Session) => void;
}) {
  const [state, setState] = useState<ForkState>({ kind: "closed" });
  const inFlight = useRef(false);
  const mounted = useRef(false);
  const anotherButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    if (state.kind === "created" && open !== false) anotherButton.current?.focus();
  }, [state.kind, open]);
  useEffect(() => {
    if (open && state.kind === "closed") void preview();
  }, [open]);
  const selectId = useId();
  const origin = session.forked_from;
  const parent = origin && sessions.find((row) => row.id === origin.parent_id);
  const children = sessions.filter((row) => row.forked_from?.parent_id === session.id);
  if (state.kind === "created" && !children.some((row) => row.id === state.child.id)) {
    children.push(state.child);
  }

  async function preview() {
    if (inFlight.current) return;
    inFlight.current = true;
    setState({ kind: "loading" });
    try {
      const result = await api.previewSessionFork(session.id);
      if (!mounted.current) return;
      const last = result.boundaries.at(-1);
      setState({ kind: "ready", preview: result, eventId: last?.event_id ?? 0, requestId: crypto.randomUUID() });
    } catch (error) {
      if (!mounted.current) return;
      setState({ kind: "error", message: error instanceof Error ? error.message : "Could not load fork boundaries." });
    } finally {
      inFlight.current = false;
    }
  }

  async function create() {
    if (state.kind !== "ready" || !state.eventId || inFlight.current) return;
    const ready = state;
    inFlight.current = true;
    setState({ ...ready, kind: "creating" });
    try {
      const child = await api.forkSession({ session_id: session.id, event_id: ready.eventId,
        revision: ready.preview.revision, request_id: ready.requestId });
      if (!mounted.current) return;
      setState({ kind: "created", child });
      onCreated(child);
    } catch (error) {
      if (!mounted.current) return;
      setState({ ...ready, error: error instanceof Error ? error.message : "Could not create fork." });
    } finally {
      inFlight.current = false;
    }
  }

  const content = <section aria-label={`Fork ${session.title}`} className="space-y-2 rounded border border-edge p-2">
    {origin && <div className="text-sm text-muted">
      Forked from {parent
        ? <button type="button" className={buttonClass} onClick={() => onSelect(parent.id)}>{parent.title}</button>
        : <span>{origin.parent_id} (not in this list)</span>}
      {` at saved message ${origin.at_event_id}`}
    </div>}
    {children.length > 0 && <div aria-label="Child sessions" className="flex flex-wrap gap-2">
      {children.map((child) => <button key={child.id} type="button" className={buttonClass}
        onClick={() => onSelect(child.id)}>Open {child.title}</button>)}
    </div>}
    {state.kind === "closed" && <button type="button" className={buttonClass} onClick={() => void preview()}>Fork session</button>}
    {state.kind === "loading" && <p role="status" className="text-sm text-muted">Loading saved boundaries...</p>}
    {state.kind === "error" && <><p role="alert">{state.message}</p>
      <button type="button" className={buttonClass} onClick={() => void preview()}>Retry loading boundaries</button></>}
    {(state.kind === "ready" || state.kind === "creating") && <>
      <p className="text-sm text-muted">Copies saved history through this message. Unsaved running work is excluded. Files in the workspace remain shared.</p>
      {state.preview.boundaries.length === 0 ? <p role="status">No saved boundary is available yet.</p> : <>
        <label htmlFor={selectId} className="block text-sm text-txt">Fork after saved message</label>
        <select id={selectId} value={state.eventId} disabled={state.kind === "creating"}
          className="w-full min-h-8 rounded bg-panel text-txt border border-edge focus-visible:outline focus-visible:outline-accent"
          onChange={(event) => setState({ ...state, kind: "ready", eventId: Number(event.target.value), requestId: crypto.randomUUID() })}>
          {state.preview.boundaries.map((boundary) => <option key={boundary.event_id} value={boundary.event_id}>
            {boundary.event_id}: {boundary.role} - {boundary.label || "Message"}
          </option>)}
        </select>
        <button type="button" className={buttonClass} disabled={state.kind === "creating"} onClick={() => void create()}>
          {state.kind === "creating" ? "Creating fork..." : "Create fork"}
        </button>
      </>}
      <button type="button" className={buttonClass} disabled={state.kind === "creating"} onClick={() => void preview()}>Refresh boundaries</button>
      <button type="button" className={buttonClass} disabled={state.kind === "creating"} onClick={() => onClose ? onClose() : setState({ kind: "closed" })}>Cancel</button>
      {state.kind === "ready" && state.error && <p role="alert">{state.error}</p>}
      {state.kind === "creating" && <span role="status" className="text-sm text-muted">Saving child session...</span>}
    </>}
    {state.kind === "created" && <>
      <p role="status">Fork created.</p>
      <button ref={anotherButton} type="button" className={buttonClass} onClick={() => void preview()}>Create another fork</button>
    </>}
  </section>;
  if (open === undefined) return content;
  return (
    <Modal
      open={open}
      onClose={() => onClose?.()}
      label={`Fork ${session.title}`}
      finalFocus={returnFocus ? { current: returnFocus } : undefined}
      backdropClassName="fixed inset-0 z-[90] bg-black/50"
      viewportClassName="fixed inset-0 z-[90] flex items-center justify-center"
      className="w-[480px] max-w-[calc(100vw-32px)] max-h-[calc(100vh-32px)] overflow-y-auto rounded-xl border border-edge bg-panel p-4 text-txt outline-none"
    >
      <header className="flex items-center justify-between gap-2 mb-3">
        <h2 className="text-sm font-semibold truncate">Fork {session.title}</h2>
        <button type="button" className={buttonClass} aria-label={`Close Fork ${session.title}`} onClick={onClose}>Close</button>
      </header>
      {content}
    </Modal>
  );
}
