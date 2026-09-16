import { useEffect, useState } from "react";
import { api, type AdviceReceipt } from "../lib/api";
import { scheduleButton } from "./ScheduleEditor";

export default function AdvicePane({ sessionId }: { sessionId: string }) {
  const [receipts, setReceipts] = useState<AdviceReceipt[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    let loading = false;
    setReceipts([]);
    setError("");
    const load = async () => {
      if (loading) return;
      loading = true;
      try {
        const result = await api.getAdvice(sessionId);
        if (active && result.session_id === sessionId) { setReceipts(result.receipts); setError(""); }
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : "Could not load advice");
      } finally { loading = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 1000);
    return () => { active = false; window.clearInterval(timer); };
  }, [sessionId]);
  return <section aria-label="Advice history" className="space-y-3 text-txt">
    <p className="text-sm text-muted">Each consultation makes one request with tools disabled. Use /advise followed by an optional question to request another opinion.</p>
    {error && <p role="alert" className="text-risk">{error}</p>}
    {!receipts.length && <p className="text-sm text-muted">No consultations recorded yet.</p>}
    {receipts.filter(r => r.session_id === sessionId).map(receipt => <article key={receipt.request_id} className="space-y-2 rounded border border-edge bg-panel2 p-3">
      <p className="text-sm">{new Date(receipt.created_at * 1000).toLocaleString()} · {receipt.status} · {receipt.model || "Resolving advisor"}</p>
      {receipt.question && <p className="text-sm font-medium">{receipt.question}</p>}
      {receipt.answer && <p className="whitespace-pre-wrap text-sm">{receipt.answer}</p>}
      {receipt.error && <p className="text-sm text-risk">{receipt.error}</p>}
      <p className="text-xs text-muted">{receipt.usage.tokens_in ?? "Unknown"} input tokens · {receipt.usage.tokens_out ?? "Unknown"} output tokens · {receipt.usage.cost_usd === null ? "Cost unknown" : `$${receipt.usage.cost_usd.toFixed(6)} (${receipt.usage.cost_source})`}</p>
      {(receipt.status === "pending" || receipt.status === "running") && <button type="button" className={scheduleButton} onClick={() => {
        void api.cancelAdvice(sessionId, receipt.request_id).then(result => {
          setReceipts(rows => rows.map(row => row.request_id === result.receipt.request_id ? result.receipt : row));
        }).catch(err => setError(err instanceof Error ? err.message : "Cancellation failed"));
      }}>Cancel consultation</button>}
      {receipt.status === "cancelling" && <p className="text-sm text-muted">Cancellation requested; waiting for the provider receipt.</p>}
    </article>)}
  </section>;
}
