import { useState, useEffect } from "react";
import { api, type PendingReview, type PendingReviewFile } from "../lib/api";
import { Check, X, Eye, AlertCircle, RefreshCw } from "lucide-react";

export default function DiffReviewPane({ reviews, onRefresh }: {
  reviews: PendingReview[];
  onRefresh: () => void;
}) {
  const [decisions, setDecisions] = useState<Record<string, "accept" | "reject">>({});
  const [loading, setLoading] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ text: string; type: "success" | "error" } | null>(null);

  useEffect(() => {
    const initial: Record<string, "accept" | "reject"> = { ...decisions };
    reviews.forEach(rev => {
      rev.files.forEach(file => {
        file.hunks.forEach(hunk => {
          if (!initial[hunk.id]) {
            initial[hunk.id] = "accept";
          }
        });
      });
    });
    setDecisions(initial);
  }, [reviews]);

  const handleSetHunkDecision = (hunkId: string, value: "accept" | "reject") => {
    setDecisions(prev => ({ ...prev, [hunkId]: value }));
  };

  const handleSetFileDecisions = (file: PendingReviewFile, value: "accept" | "reject") => {
    const updated = { ...decisions };
    file.hunks.forEach(h => {
      updated[h.id] = value;
    });
    setDecisions(updated);
  };

  const handleApply = async (reviewId: string) => {
    setLoading(reviewId);
    setMsg(null);
    try {
      const res = await api.applyReview(reviewId, decisions);
      if (res.ok) {
        setMsg({ text: res.message || "Applied successfully", type: "success" });
        window.dispatchEvent(new Event("harness-repo-mutated"));
        window.dispatchEvent(new Event("harness-config-changed"));
        onRefresh();
      } else {
        setMsg({ text: res.message || "Failed to apply", type: "error" });
      }
    } catch (err: any) {
      setMsg({ text: err.message || "Error applying review", type: "error" });
    } finally {
      setLoading(null);
    }
  };

  const handleDismiss = async (reviewId: string) => {
    setLoading(reviewId);
    setMsg(null);
    try {
      const res = await api.dismissReview(reviewId);
      if (res.ok) {
        setMsg({ text: "Review dismissed", type: "success" });
        onRefresh();
      } else {
        setMsg({ text: "Failed to dismiss review", type: "error" });
      }
    } catch (err: any) {
      setMsg({ text: err.message || "Error dismissing review", type: "error" });
    } finally {
      setLoading(null);
    }
  };

  if (reviews.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-6 text-center text-muted">
        <Eye size={24} className="mb-2 text-faint" />
        <span className="text-xs font-medium">No pending edits to review</span>
        <span className="text-[10px] text-faint mt-1">Enable "Review edits before applying" in Settings.</span>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-3 space-y-4 select-text">
      {msg && (
        <div className={`p-2 rounded text-[11px] flex items-start gap-1.5 ${
          msg.type === "success" ? "bg-accent/10 border border-accent/20 text-accent" : "bg-risk/10 border border-risk/20 text-risk"
        }`}>
          <AlertCircle size={12} className="shrink-0 mt-0.5" />
          <span>{msg.text}</span>
        </div>
      )}

      {reviews.map(rev => {
        // Calculate totals for this review
        let totalHunks = 0;
        let acceptedHunks = 0;
        rev.files.forEach(f => {
          f.hunks.forEach(h => {
            totalHunks++;
            if (decisions[h.id] === "accept") {
              acceptedHunks++;
            }
          });
        });

        return (
          <div key={rev.id} className="bg-panel2/40 border border-edge rounded p-3 space-y-3">
            <div className="flex flex-col gap-1 border-b border-edge/60 pb-2">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-mono text-accent uppercase font-bold tracking-wider bg-accent/10 px-1.5 py-0.5 rounded">
                  {rev.id}
                </span>
                <span className="text-[9px] text-faint font-mono">
                  {new Date(rev.created_at * 1000).toLocaleTimeString()}
                </span>
              </div>
              <span className="text-xs font-semibold text-txt">{rev.objective}</span>
              <span className="text-[10px] text-muted font-mono leading-tight">Job ID: {rev.job_id.slice(0, 12)}...</span>
            </div>

            <div className="space-y-4">
              {rev.files.map((file, fIdx) => (
                <div key={fIdx} className="space-y-2">
                  <div className="flex items-center justify-between border-b border-edge/30 pb-1">
                    <span className="text-[11px] font-mono text-muted truncate max-w-[180px]" title={file.path}>
                      {file.path}
                    </span>
                    <div className="flex gap-1.5">
                      <button
                        onClick={() => handleSetFileDecisions(file, "accept")}
                        className="text-[9px] px-1.5 py-0.5 rounded bg-panel border border-edge text-accent hover:bg-accent/10 hover:border-accent/30 transition font-medium"
                      >
                        Accept All
                      </button>
                      <button
                        onClick={() => handleSetFileDecisions(file, "reject")}
                        className="text-[9px] px-1.5 py-0.5 rounded bg-panel border border-edge text-faint hover:text-risk hover:bg-risk/10 hover:border-risk/30 transition font-medium"
                      >
                        Reject All
                      </button>
                    </div>
                  </div>

                  <div className="space-y-2">
                    {file.hunks.map(hunk => {
                      const isAccepted = decisions[hunk.id] === "accept";
                      return (
                        <div key={hunk.id} className={`border rounded overflow-hidden transition-colors ${
                          isAccepted ? "border-edge" : "border-edge/40 opacity-70"
                        }`}>
                          <div className="bg-panel flex items-center justify-between px-2 py-1 border-b border-edge/50">
                            <span className="text-[9px] font-mono text-faint">{hunk.header.trim()}</span>
                            <div className="flex gap-1">
                              <button
                                onClick={() => handleSetHunkDecision(hunk.id, "accept")}
                                className={`p-1 rounded transition-colors ${
                                  isAccepted ? "bg-accent/20 text-accent" : "hover:bg-panel2 text-faint"
                                }`}
                                title="Accept Hunk"
                              >
                                <Check size={10} />
                              </button>
                              <button
                                onClick={() => handleSetHunkDecision(hunk.id, "reject")}
                                className={`p-1 rounded transition-colors ${
                                  !isAccepted ? "bg-risk/20 text-risk" : "hover:bg-panel2 text-faint"
                                }`}
                                title="Reject Hunk"
                              >
                                <X size={10} />
                              </button>
                            </div>
                          </div>

                          <pre className="p-2 overflow-x-auto text-[10px] font-mono leading-relaxed bg-black/30 max-h-[200px] scrollbar-thin">
                            {hunk.lines.map((line, lIdx) => {
                              const isAdd = line.startsWith("+");
                              const isDel = line.startsWith("-");
                              const lineClass = isAdd
                                ? "bg-accent/10 text-accent border-l-2 border-accent/40 px-1"
                                : isDel
                                ? "bg-risk/10 text-risk border-l-2 border-risk/40 px-1"
                                : "text-muted px-1";
                              return (
                                <div key={lIdx} className={lineClass}>
                                  {line}
                                </div>
                              );
                            })}
                          </pre>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>

            <div className="flex gap-2 pt-2 border-t border-edge/60">
              <button
                onClick={() => handleApply(rev.id)}
                disabled={loading !== null}
                className="flex-1 py-1.5 px-3 rounded bg-accent text-panel font-semibold text-xs hover:bg-accent/90 disabled:opacity-50 transition flex items-center justify-center gap-1.5"
              >
                {loading === rev.id ? (
                  <RefreshCw size={11} className="animate-spin" />
                ) : null}
                Apply Selected ({acceptedHunks}/{totalHunks})
              </button>
              <button
                onClick={() => handleDismiss(rev.id)}
                disabled={loading !== null}
                className="py-1.5 px-3 rounded bg-panel border border-edge text-muted text-xs hover:text-risk hover:bg-risk/10 hover:border-risk/30 disabled:opacity-50 transition"
              >
                Dismiss
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
