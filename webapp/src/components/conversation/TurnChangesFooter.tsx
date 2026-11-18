import { useRef, useState } from "react";
import { api } from "../../lib/api";
import { openAgentFile } from "../../lib/agentLinks";
import { EditDiffCounts, EditDiffPatch } from "./EditDiff";

/** One file the turn wrote: summed counts and the patch of each write. */
export type TurnChangedFile = {
  path: string;
  added: number;
  removed: number;
  patches: { patch: string; truncated?: boolean }[];
};

/**
 * End of a finished turn that wrote files: "2 files changed · Review · Undo".
 * Undo restores the turn's first restore point (taken before its first
 * write). The restore takes its own snapshot first, so Redo puts the
 * changes back.
 */
export type TurnChangesItem = {
  kind: "turn_changes";
  files: TurnChangedFile[];
  /** Restore point before the turn's first write; null when none was taken. */
  checkpointId: string | null;
  /** Only the newest turn with changes, with no turn open, offers Undo. */
  canUndo: boolean;
};

type UndoState =
  | { phase: "idle" }
  | { phase: "busy" }
  | { phase: "undone"; redoId: string }
  | { phase: "error"; message: string };

function filesLabel(n: number): string {
  return n === 1 ? "1 file changed" : `${n} files changed`;
}

const LINK_BUTTON =
  "bg-transparent border-0 p-0 font-sans text-ui-11 cursor-pointer text-muted hover:text-txt hover:underline underline-offset-2 disabled:cursor-default disabled:opacity-50";

export function TurnChangesFooter({ item }: { item: TurnChangesItem }) {
  const [reviewOpen, setReviewOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const [undo, setUndo] = useState<UndoState>({ phase: "idle" });
  const added = item.files.reduce((n, f) => n + f.added, 0);
  const removed = item.files.reduce((n, f) => n + f.removed, 0);

  const restore = async (id: string, next: (redoId: string) => UndoState) => {
    setUndo({ phase: "busy" });
    try {
      const res = await api.restoreCheckpoint(id);
      if (!res.ok) throw new Error("Restore failed");
      setUndo(next(res.auto_snapshot_id || ""));
      window.dispatchEvent(new Event("harness-repo-mutated"));
    } catch (err) {
      setUndo({ phase: "error", message: err instanceof Error ? err.message : "Restore failed" });
    }
  };

  // A long review scrolls the footer row out of view (the bar is then stuck
  // below the row start). Scroll back to the row before it shrinks: after
  // the shrink, the transcript virtualizer re-measures the row and resets
  // the scroll offset, so a later scroll does not hold.
  const hideReview = () => {
    const root = rootRef.current;
    const bar = barRef.current;
    if (root && bar && bar.getBoundingClientRect().top - root.getBoundingClientRect().top > 1) {
      root.scrollIntoView({ block: "start" });
    }
    setReviewOpen(false);
  };

  const undoId = item.canUndo ? item.checkpointId : null;
  return (
    <div ref={rootRef} className="flex flex-col pb-1 scroll-mt-3 text-ui-11 font-sans select-none" data-testid="turn-changes">
      {/* Sticky while open: Hide and Undo stay reachable inside a long diff. */}
      <div
        ref={barRef}
        className={`flex items-center gap-2 pt-1 text-faint ${reviewOpen ? "sticky top-0 z-10 bg-bg pb-1 border-b border-edge/40" : ""}`}
        data-testid="turn-changes-bar"
      >
        <span className="text-muted">{filesLabel(item.files.length)}</span>
        <EditDiffCounts added={added} removed={removed} />
        <span aria-hidden>·</span>
        <button
          type="button"
          className={LINK_BUTTON}
          aria-expanded={reviewOpen}
          data-testid="turn-changes-review"
          onClick={() => (reviewOpen ? hideReview() : setReviewOpen(true))}
        >
          {reviewOpen ? "Hide" : "Review"}
        </button>
        {undoId && undo.phase !== "undone" ? (
          <>
            <span aria-hidden>·</span>
            <button
              type="button"
              className={LINK_BUTTON}
              data-testid="turn-changes-undo"
              disabled={undo.phase === "busy"}
              title="Put the files back as they were before this turn"
              onClick={() => void restore(undoId, (redoId) => ({ phase: "undone", redoId }))}
            >
              Undo
            </button>
          </>
        ) : null}
        {undo.phase === "undone" ? (
          <>
            <span aria-hidden>·</span>
            <span className="text-muted" data-testid="turn-changes-undone">Changes undone</span>
            {undo.redoId ? (
              <button
                type="button"
                className={LINK_BUTTON}
                data-testid="turn-changes-redo"
                onClick={() => void restore(undo.redoId, () => ({ phase: "idle" }))}
              >
                Redo
              </button>
            ) : null}
          </>
        ) : null}
        {undo.phase === "error" ? <span className="text-risk/85">{undo.message}</span> : null}
      </div>
      {reviewOpen ? (
        <ul className="mt-1 ml-1 pl-3 border-l border-edge/50 space-y-1.5" data-testid="turn-changes-files">
          {item.files.map((f) => (
            <li key={f.path} className="min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                <button
                  type="button"
                  className="truncate min-w-0 bg-transparent border-0 p-0 font-mono text-ui-11 text-txt/80 cursor-pointer hover:text-accent/85 hover:underline underline-offset-2 text-left"
                  title={`Open ${f.path}`}
                  onClick={() => openAgentFile(f.path)}
                >
                  {f.path}
                </button>
                <EditDiffCounts added={f.added} removed={f.removed} />
              </div>
              {f.patches.map((p, i) => (p.patch ? <EditDiffPatch key={i} patch={p.patch} truncated={p.truncated} /> : null))}
            </li>
          ))}
          <li>
            <button
              type="button"
              className={LINK_BUTTON}
              data-testid="turn-changes-hide-end"
              onClick={hideReview}
            >
              Hide changes
            </button>
          </li>
        </ul>
      ) : null}
    </div>
  );
}
