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
  const [hunkStates, setHunkStates] = useState<Record<string, "idle" | "applying" | "applied" | "fading-out">>({});

  const prefersReduced = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

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
    const review = reviews.find(r => r.id === reviewId);
    if (!review) return;

    setLoading(reviewId);
    setMsg(null);

    // Identify all hunks in this review and their decisions
    const allHunks: { id: string; decision: "accept" | "reject" }[] = [];
    review.files.forEach(f => {
      f.hunks.forEach(h => {
        allHunks.push({ id: h.id, decision: decisions[h.id] || "accept" });
      });
    });

    const accepted = allHunks.filter(h => h.decision === "accept");
    const rejected = allHunks.filter(h => h.decision === "reject");

    // Initialize animation states
    const initialStates: Record<string, "idle" | "applying" | "applied" | "fading-out"> = {};
    if (prefersReduced) {
      // With reduced motion, immediately mark accepted as applied, rejected as fading-out
      accepted.forEach(h => {
        initialStates[h.id] = "applied";
      });
      rejected.forEach(h => {
        initialStates[h.id] = "fading-out";
      });
    } else {
      // Staggered/normal motion:
      rejected.forEach(h => {
        initialStates[h.id] = "fading-out";
      });
      accepted.forEach(h => {
        initialStates[h.id] = "idle";
      });
    }
    setHunkStates(prev => ({ ...prev, ...initialStates }));

    let isFailed = false;

    // Fire the POST immediately
    const apiPromise = api.applyReview(reviewId, decisions).then(res => {
      if (!res.ok) {
        throw new Error(res.message || "Failed to apply");
      }
      return res;
    }).catch(err => {
      isFailed = true;
      // If error, immediately clear animation state to restore original UI
      setHunkStates(prev => {
        const restored = { ...prev };
        allHunks.forEach(h => {
          delete restored[h.id];
        });
        return restored;
      });
      throw err;
    });

    if (prefersReduced) {
       try {
         const res = await apiPromise;
         setMsg({ text: res.message || "Applied successfully", type: "success" });
         window.dispatchEvent(new Event("harness-repo-mutated"));
         window.dispatchEvent(new Event("harness-config-changed"));
         onRefresh();
       } catch (err: any) {
         setMsg({ text: `Apply failed: ${err.message || "Error applying review"}`, type: "error" });
       } finally {
         setLoading(null);
       }
       return;
     }

    // Normal motion: staggered cascade.
    // Total animation should not exceed ~1.2s.
    // If we have many hunks, we clamp the stagger duration so the total cascading remains snappy.
    const maxCascadeTime = 600; // max ms to stagger all hunks
    const staggerDelay = accepted.length > 1 ? Math.min(150, maxCascadeTime / (accepted.length - 1)) : 100;
    const sweepDuration = 300; // ms for the sweep overlay

    // Start cascading the "applying" state
    const animationPromise = new Promise<void>((resolve) => {
      if (accepted.length === 0) {
        // Only rejected hunks: let them fade for 300ms, then resolve
        setTimeout(() => {
          resolve();
        }, 300);
        return;
      }

      let completedCount = 0;
      accepted.forEach((h, index) => {
        const startDelay = index * staggerDelay;
        setTimeout(() => {
          if (isFailed) return;
          setHunkStates(prev => ({ ...prev, [h.id]: "applying" }));

          // After sweep duration, we mark as applied (ready for green check)
          setTimeout(() => {
            if (isFailed) return;
            setHunkStates(prev => ({ ...prev, [h.id]: "applied" }));
            completedCount++;
            if (completedCount === accepted.length) {
              // Give the green checkmark 250ms before resolve (then it collapses)
              setTimeout(() => {
                resolve();
              }, 250);
            }
          }, sweepDuration);
        }, startDelay);
      });
    });

    try {
      // Wait for both the backend apply request AND the initial animation phase
      const [res] = await Promise.all([apiPromise, animationPromise]);

      // If both completed successfully:
      setMsg({ text: res.message || "Applied successfully", type: "success" });
      window.dispatchEvent(new Event("harness-repo-mutated"));
      window.dispatchEvent(new Event("harness-config-changed"));
      
      // We are about to refresh, but let's wait a tiny bit (300ms) for the collapse transitions of "applied" and "fading-out" to complete
      setTimeout(() => {
        onRefresh();
      }, 300);
    } catch (err: any) {
      setMsg({ text: `Apply failed: ${err.message || "Error applying review"}`, type: "error" });
    } finally {
      setLoading(null);
      // Clean up hunkStates
      setHunkStates(prev => {
        const cleaned = { ...prev };
        allHunks.forEach(h => {
          delete cleaned[h.id];
        });
        return cleaned;
      });
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
      <style>{`
        @keyframes green-sweep-overlay {
          0% { transform: translateY(-100%); }
          100% { transform: translateY(200%); }
        }
        .animate-sweep-overlay {
          animation: green-sweep-overlay 1.2s infinite linear;
        }
        @keyframes scale-up {
          0% { transform: scale(0.6); opacity: 0; }
          50% { transform: scale(1.1); }
          100% { transform: scale(1); opacity: 1; }
        }
        .animate-scale-up {
          animation: scale-up 0.25s cubic-bezier(0.175, 0.885, 0.32, 1.275) forwards;
        }
      `}</style>
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
                      const hState = hunkStates[hunk.id];
                      const isApplying = hState === "applying";
                      const isApplied = hState === "applied";
                      const isFadingOut = hState === "fading-out";

                      // Style/class for hunk container
                      let containerStyle: React.CSSProperties = {};
                      let containerClass = "relative border rounded overflow-hidden transition-all duration-300 ";

                      if (isApplied || isFadingOut) {
                        containerClass += "opacity-0 scale-95 pointer-events-none ";
                        containerStyle = {
                          maxHeight: "0px",
                          marginTop: "0px",
                          marginBottom: "0px",
                          paddingTop: "0px",
                          paddingBottom: "0px",
                          borderWidth: "0px",
                          transition: prefersReduced
                            ? "opacity 150ms ease"
                            : "max-height 350ms cubic-bezier(0.4, 0, 0.2, 1), opacity 300ms ease, margin 350ms, padding 350ms, border-width 350ms"
                        };
                      } else {
                        containerClass += isAccepted ? "border-edge" : "border-edge/40 opacity-70";
                        containerStyle = {
                          maxHeight: "600px"
                        };
                      }

                      return (
                        <div
                          key={hunk.id}
                          className={containerClass}
                          style={containerStyle}
                        >
                          {/* Green Sweep Overlay */}
                          {isApplying && !prefersReduced && (
                            <div className="absolute inset-0 pointer-events-none overflow-hidden z-10">
                              <div
                                className="absolute inset-x-0 h-1/2 bg-gradient-to-b from-transparent via-accent/20 to-transparent animate-sweep-overlay"
                                style={{
                                  background: "linear-gradient(to bottom, transparent, rgba(63, 185, 80, 0.25), transparent)"
                                }}
                              />
                            </div>
                          )}

                          {/* Green Applied Checkmark Overlay */}
                          {isApplied && !prefersReduced && (
                            <div className="absolute inset-0 bg-accent/5 flex items-center justify-center z-20">
                              <div className="bg-panel border border-accent/40 rounded-full p-2 text-accent shadow-lg shadow-accent/10 animate-scale-up">
                                <Check size={18} className="stroke-[3]" />
                              </div>
                            </div>
                          )}

                          <div className="bg-panel flex items-center justify-between px-2 py-1 border-b border-edge/50">
                            <span className="text-[9px] font-mono text-faint">{hunk.header.trim()}</span>
                            <div className="flex gap-1">
                              <button
                                onClick={() => handleSetHunkDecision(hunk.id, "accept")}
                                disabled={loading !== null}
                                className={`p-1 rounded transition-colors ${
                                  isAccepted ? "bg-accent/20 text-accent" : "hover:bg-panel2 text-faint"
                                }`}
                                title="Accept Hunk"
                              >
                                <Check size={10} />
                              </button>
                              <button
                                onClick={() => handleSetHunkDecision(hunk.id, "reject")}
                                disabled={loading !== null}
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
                              let lineClass = "";
                              if (isAdd) {
                                lineClass = isApplying
                                  ? "bg-accent/25 text-accent border-l-2 border-accent/80 px-1 transition-all duration-300"
                                  : "bg-accent/10 text-accent border-l-2 border-accent/40 px-1 transition-all duration-300";
                              } else if (isDel) {
                                lineClass = "bg-risk/10 text-risk border-l-2 border-risk/40 px-1";
                              } else {
                                lineClass = "text-muted px-1";
                              }
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
                  <>
                    <RefreshCw size={11} className="animate-spin" />
                    Applying {acceptedHunks} hunk{acceptedHunks === 1 ? "" : "s"}...
                  </>
                ) : (
                  <>
                    Apply Selected ({acceptedHunks}/{totalHunks})
                  </>
                )}
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
