import {
  Decoration,
  EditorView,
  ViewPlugin,
  WidgetType,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";
import { RangeSetBuilder, type Extension } from "@codemirror/state";
import type { InFilePendingHunk } from "../lib/inFileReview";

export type InFileReviewHandlers = {
  onAccept: (item: InFilePendingHunk) => void;
  onReject: (item: InFilePendingHunk) => void;
  applyingKey: string | null;
};

class HunkActionWidget extends WidgetType {
  private readonly item: InFilePendingHunk;
  private readonly handlers: InFileReviewHandlers;

  constructor(item: InFilePendingHunk, handlers: InFileReviewHandlers) {
    super();
    this.item = item;
    this.handlers = handlers;
  }

  eq(other: HunkActionWidget): boolean {
    return (
      this.item.decisionKey === other.item.decisionKey &&
      this.handlers.applyingKey === other.handlers.applyingKey &&
      this.item.hunk.header === other.item.hunk.header
    );
  }

  toDOM(): HTMLElement {
    const root = document.createElement("div");
    root.className = "cm-infile-hunk-actions";
    root.setAttribute("data-testid", "infile-hunk-actions");
    root.setAttribute("data-hunk-key", this.item.decisionKey);

    const label = document.createElement("span");
    label.className = "cm-infile-hunk-label";
    label.textContent = (this.item.hunk.header || "").trim() || "pending hunk";
    root.appendChild(label);

    const busy = this.handlers.applyingKey === this.item.decisionKey;
    const disabled = this.handlers.applyingKey != null;

    const accept = document.createElement("button");
    accept.type = "button";
    accept.className = "cm-infile-hunk-btn cm-infile-hunk-accept";
    accept.textContent = busy ? "Applying…" : "Accept";
    accept.title = "Accept hunk";
    accept.disabled = disabled;
    accept.setAttribute("data-testid", "infile-hunk-accept");
    accept.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
    accept.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (disabled) return;
      this.handlers.onAccept(this.item);
    });

    const reject = document.createElement("button");
    reject.type = "button";
    reject.className = "cm-infile-hunk-btn cm-infile-hunk-reject";
    reject.textContent = "Reject";
    reject.title = "Reject hunk";
    reject.disabled = disabled;
    reject.setAttribute("data-testid", "infile-hunk-reject");
    reject.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
    reject.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (disabled) return;
      this.handlers.onReject(this.item);
    });

    root.appendChild(accept);
    root.appendChild(reject);
    return root;
  }

  ignoreEvent(): boolean {
    return true;
  }
}

function buildDecorations(
  view: EditorView,
  hunks: InFilePendingHunk[],
  handlers: InFileReviewHandlers,
): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  const doc = view.state.doc;
  const lineCount = doc.lines;
  const pending: { from: number; to: number; deco: Decoration }[] = [];

  for (const item of hunks) {
    const anchorLine = Math.max(1, Math.min(item.geometry.anchorOldLine, lineCount));
    const lineObj = doc.line(anchorLine);
    pending.push({
      from: lineObj.from,
      to: lineObj.from,
      deco: Decoration.widget({
        widget: new HunkActionWidget(item, handlers),
        side: -1,
        block: true,
      }),
    });

    const marked = new Set<number>();
    for (const oldLine of item.geometry.oldLines) {
      if (oldLine < 1 || oldLine > lineCount || marked.has(oldLine)) continue;
      marked.add(oldLine);
      const kindEntry = item.geometry.lineKinds.find(
        (lk) => lk.oldLine === oldLine && (lk.kind === "del" || lk.kind === "context"),
      );
      const cls =
        kindEntry?.kind === "del" ? "cm-infile-hunk-del" : "cm-infile-hunk-ctx";
      const target = doc.line(oldLine);
      pending.push({
        from: target.from,
        to: target.from,
        deco: Decoration.line({ class: cls }),
      });
    }
  }

  pending.sort((a, b) => a.from - b.from || a.to - b.to);
  for (const row of pending) {
    builder.add(row.from, row.to, row.deco);
  }

  return builder.finish();
}

const infileReviewTheme = EditorView.baseTheme({
  ".cm-infile-hunk-actions": {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    padding: "4px 8px",
    margin: "2px 0",
    fontSize: "11px",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
    backgroundColor: "rgba(63, 185, 80, 0.08)",
    border: "1px solid rgba(63, 185, 80, 0.25)",
    borderRadius: "4px",
  },
  ".cm-infile-hunk-label": {
    flex: "1 1 auto",
    minWidth: 0,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    color: "var(--cm-infile-muted, #8b949e)",
  },
  ".cm-infile-hunk-btn": {
    flex: "0 0 auto",
    borderRadius: "3px",
    border: "1px solid rgba(110, 118, 129, 0.4)",
    background: "transparent",
    color: "inherit",
    fontSize: "10px",
    fontWeight: 600,
    padding: "2px 8px",
    cursor: "pointer",
  },
  ".cm-infile-hunk-btn:disabled": {
    opacity: 0.45,
    cursor: "not-allowed",
  },
  ".cm-infile-hunk-accept": {
    color: "#3fb950",
    borderColor: "rgba(63, 185, 80, 0.45)",
  },
  ".cm-infile-hunk-reject": {
    color: "#f85149",
    borderColor: "rgba(248, 81, 73, 0.45)",
  },
  ".cm-infile-hunk-del": {
    backgroundColor: "rgba(248, 81, 73, 0.12)",
  },
  ".cm-infile-hunk-ctx": {
    backgroundColor: "rgba(63, 185, 80, 0.06)",
  },
});

/**
 * CodeMirror extension that paints pending review hunks and Accept/Reject
 * controls inside the open file. No confirm dialogs — revert remains cheap.
 */
export function createInFileReviewExtension(
  hunks: InFilePendingHunk[],
  handlers: InFileReviewHandlers,
): Extension {
  if (!hunks.length) return [];

  const plugin = ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;

      constructor(view: EditorView) {
        this.decorations = buildDecorations(view, hunks, handlers);
      }

      update(update: ViewUpdate) {
        if (update.docChanged || update.viewportChanged) {
          this.decorations = buildDecorations(update.view, hunks, handlers);
        }
      }
    },
    {
      decorations: (v) => v.decorations,
    },
  );

  return [infileReviewTheme, plugin];
}
