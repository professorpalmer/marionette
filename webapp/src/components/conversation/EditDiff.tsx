/** Line diff receipt of one agent file write (server `harness/edit_diff.py`). */
export type EditDiffData = {
  added: number;
  removed: number;
  patch: string;
  truncated?: boolean;
};

/** Parse the `diff` field of an action result. Anything else is no diff. */
export function parseEditDiff(raw: unknown): EditDiffData | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.added !== "number" || typeof r.removed !== "number") return null;
  return {
    added: r.added,
    removed: r.removed,
    patch: typeof r.patch === "string" ? r.patch : "",
    truncated: r.truncated === true,
  };
}

/** "+12 -3" in the status hues. Zero sides stay out. */
export function EditDiffCounts({ added, removed }: { added: number; removed: number }) {
  if (!added && !removed) return null;
  return (
    <span className="shrink-0 inline-flex gap-1 tabular-nums text-ui-11" data-testid="edit-diff-counts">
      {added ? <span className="text-good/85">+{added}</span> : null}
      {removed ? <span className="text-risk/85">-{removed}</span> : null}
    </span>
  );
}

function lineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "text-faint/70";
  if (line.startsWith("+")) return "text-good bg-good/10";
  if (line.startsWith("-")) return "text-risk bg-risk/10";
  if (line.startsWith("@@")) return "text-faint/80";
  return "text-txt/75";
}

/** The unified patch, one colored row per line. */
export function EditDiffPatch({ patch, truncated }: { patch: string; truncated?: boolean }) {
  const lines = patch.replace(/\n$/, "").split("\n");
  return (
    <div data-testid="edit-diff-patch">
      <pre className="mt-1 max-h-72 overflow-auto font-mono text-ui-10 leading-snug bg-panel/60 border border-edge/40 rounded py-1">
        {lines.map((line, i) => (
          <div key={i} className={`px-2 whitespace-pre-wrap break-all ${lineClass(line)}`}>
            {line || " "}
          </div>
        ))}
      </pre>
      {truncated ? <div className="mt-0.5 text-ui-10 text-faint">Diff cut. Open the file for the full change.</div> : null}
    </div>
  );
}
