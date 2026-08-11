/**
 * Thin read-only peek for spilled tool stdout (spill:// URIs).
 * No editor chrome — just the URI, size, and scrollable body.
 */

import { useEffect } from "react";
import { X } from "lucide-react";

export type SpillPreviewState = {
  uri: string;
  content: string;
  chars: number;
  truncated: boolean;
  error?: string;
};

export default function SpillPreviewModal({
  preview,
  onClose,
}: {
  preview: SpillPreviewState | null;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!preview) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [preview, onClose]);

  if (!preview) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}
      data-testid="spill-preview-modal"
    >
      <div
        className="relative w-[min(920px,92vw)] max-h-[85vh] flex flex-col bg-panel border border-edge rounded-lg shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-3 py-2 border-b border-edge/60">
          <div className="min-w-0 flex-1">
            <div className="font-mono text-[11px] text-accent/90 truncate" title={preview.uri}>
              {preview.uri}
            </div>
            <div className="text-[10px] text-faint/70 tabular-nums">
              {preview.error
                ? "Failed to load"
                : `${preview.chars.toLocaleString()} chars${
                    preview.truncated ? " · preview truncated" : ""
                  }`}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 text-faint hover:text-txt bg-transparent border border-edge/50 rounded-full transition-colors"
            title="Close"
          >
            <X size={14} />
          </button>
        </div>
        <pre className="m-0 px-3 py-2 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-snug text-txt/90 min-h-[12rem]">
          {preview.error || preview.content || "(empty)"}
        </pre>
      </div>
    </div>
  );
}
