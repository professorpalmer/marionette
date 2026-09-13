import type { QueueRecovery } from "../../lib/api";

export default function QueueRecoveryNotice({ entries, onCopy }: {
  entries: QueueRecovery[];
  onCopy?: (text: string) => void;
}) {
  return <>{entries.map((entry) => (
    <details key={entry.path} className="mb-2 rounded-xl border border-edge bg-panel2/40 px-3 pb-3 text-xs leading-relaxed text-muted">
      <summary className="min-h-11 cursor-pointer py-2.5 font-medium text-txt focus-visible:outline-2 focus-visible:outline-accent">{entry.kind === "legacy" ? "Unassigned legacy prompts" : "Prompt queue needs recovery"}</summary>
      <p>Original file retained. This content will not run automatically. Review it before sending.</p>
      <p className="break-all text-faint">{entry.path}</p>
      {entry.content === null ? <p>File could not be read. Restore read access to inspect it.</p> : <>
        <pre className="my-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-edge bg-panel px-3 py-2 font-[inherit] text-txt">{entry.content}</pre>
        <button className="mt-2 inline-flex min-h-11 items-center rounded-lg border border-edge2 bg-panel px-3 text-txt hover:bg-panel2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-40 disabled:pointer-events-none" type="button" disabled={!onCopy} onClick={() => onCopy?.(entry.content ?? "")}>Copy to composer for review</button>
      </>}
    </details>
  ))}</>;
}
