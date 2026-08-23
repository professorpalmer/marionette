import { useState } from "react";
import { Check } from "lucide-react";

/** Copyable request correlation id for failed-turn support. */
export default function TraceCopy({ correlationId }: { correlationId: string }) {
  const [copied, setCopied] = useState(false);
  const id = String(correlationId || "").trim();
  if (!id) return null;

  const handleCopy = () => {
    void navigator.clipboard?.writeText(id).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      data-testid="trace-copy"
      className="text-[10px] font-mono text-faint/90 hover:text-muted truncate max-w-[28ch] flex items-center gap-1"
      title="Copy trace id for support"
    >
      <span className="truncate">Trace: {id}</span>
      {copied ? <Check size={11} className="text-good shrink-0" /> : null}
    </button>
  );
}
