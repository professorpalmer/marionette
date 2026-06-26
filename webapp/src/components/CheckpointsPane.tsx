import { useState, useEffect } from "react";
import { History, Play, ShieldAlert, Check, RefreshCw, Eye, EyeOff } from "lucide-react";
import { api, type Checkpoint, type CheckpointDiff } from "../lib/api";

export default function CheckpointsPane() {
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isRestoring, setIsRestoring] = useState<string | null>(null);
  const [snapshotLabel, setSnapshotLabel] = useState("");
  const [isCreatingSnapshot, setIsCreatingSnapshot] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const [expandedDiffs, setExpandedDiffs] = useState<Record<string, boolean>>({});
  const [diffData, setDiffData] = useState<Record<string, CheckpointDiff>>({});
  const [loadingDiffs, setLoadingDiffs] = useState<Record<string, boolean>>({});

  const fetchCheckpoints = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const list = await api.getCheckpoints();
      // Sort newest first
      const sorted = [...list].sort((a, b) => b.timestamp - a.timestamp);
      setCheckpoints(sorted);
    } catch (err: any) {
      setError(err?.message || "Failed to fetch checkpoints");
    } finally {
      setIsLoading(false);
    }
  };

  const toggleDiff = async (id: string) => {
    const isCurrentlyExpanded = !!expandedDiffs[id];
    setExpandedDiffs((prev) => ({ ...prev, [id]: !isCurrentlyExpanded }));

    if (!isCurrentlyExpanded && !diffData[id]) {
      setLoadingDiffs((prev) => ({ ...prev, [id]: true }));
      try {
        const res = await api.getCheckpointDiff(id);
        setDiffData((prev) => ({ ...prev, [id]: res }));
      } catch (err: any) {
        setDiffData((prev) => ({
          ...prev,
          [id]: {
            ok: false,
            diff: "",
            files: [],
            truncated: false,
            error: err?.message || "Failed to fetch diff",
          },
        }));
      } finally {
        setLoadingDiffs((prev) => ({ ...prev, [id]: false }));
      }
    }
  };

  useEffect(() => {
    fetchCheckpoints();
  }, []);

  const handleCreateSnapshot = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!snapshotLabel.trim()) return;

    setIsCreatingSnapshot(true);
    setError(null);
    setSuccessMsg(null);
    try {
      const res = await api.snapshotCheckpoint(snapshotLabel.trim());
      if (res.ok) {
        setSnapshotLabel("");
        setSuccessMsg("Snapshot created successfully");
        fetchCheckpoints();
        setTimeout(() => setSuccessMsg(null), 3000);
      } else {
        setError("Failed to create snapshot");
      }
    } catch (err: any) {
      setError(err?.message || "Failed to create snapshot");
    } finally {
      setIsCreatingSnapshot(false);
    }
  };

  const handleRestore = async (cp: Checkpoint) => {
    const confirmRestore = window.confirm(
      `Are you sure you want to restore the workspace to: "${cp.label}"?\n\nThis will modify files in your working tree. Current uncommitted changes will be auto-saved in a new snapshot first, so you can undo this restore.`
    );
    if (!confirmRestore) return;

    setIsRestoring(cp.id);
    setError(null);
    setSuccessMsg(null);
    try {
      const res = await api.restoreCheckpoint(cp.id);
      if (res.ok) {
        setSuccessMsg(`Restored workspace. Created undo checkpoint: ${res.auto_snapshot_id.slice(0, 8)}`);
        fetchCheckpoints();
        // Since files restored, notify window to refresh file tree/source control if any listeners exist
        window.dispatchEvent(new Event("harness-repo-mutated"));
      } else {
        setError("Restore failed");
      }
    } catch (err: any) {
      setError(err?.message || "Restore failed");
    } finally {
      setIsRestoring(null);
    }
  };

  const formatTime = (timestamp: number) => {
    const date = new Date(timestamp * 1000);
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) + " " + date.toLocaleDateString();
  };

  const formatTrigger = (trigger: string) => {
    switch (trigger) {
      case "write_file":
        return "Write File";
      case "swarm_patch":
        return "Swarm Patch";
      case "manual":
        return "Manual";
      case "restore_checkpoint":
        return "Pre-Restore";
      default:
        return trigger;
    }
  };

  return (
    <div className="flex flex-col h-full bg-panel text-txt text-xs">
      {/* Header */}
      <div className="p-3 border-b border-edge flex items-center justify-between bg-panel2/15 shrink-0">
        <div className="flex items-center gap-1.5 font-semibold text-muted">
          <History size={14} className="text-accent" />
          <span>Restore Points</span>
        </div>
        <button
          onClick={fetchCheckpoints}
          disabled={isLoading}
          title="Refresh checkpoints"
          className="p-1 hover:bg-edge/50 rounded text-faint hover:text-muted transition-colors"
        >
          <RefreshCw size={12} className={isLoading ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Manual Snapshot Form */}
      <form onSubmit={handleCreateSnapshot} className="p-3 border-b border-edge bg-panel2/5 flex flex-col gap-2 shrink-0">
        <div className="text-[10px] uppercase tracking-wider text-faint font-semibold">
          Take Manual Snapshot
        </div>
        <div className="flex gap-1.5">
          <input
            type="text"
            placeholder="Label e.g. Before changing UI..."
            value={snapshotLabel}
            onChange={(e) => setSnapshotLabel(e.target.value)}
            disabled={isCreatingSnapshot}
            className="flex-1 px-2.5 py-1.5 bg-panel2 border border-edge rounded text-txt placeholder-faint focus:outline-none focus:border-accent/50 text-xs"
          />
          <button
            type="submit"
            disabled={isCreatingSnapshot || !snapshotLabel.trim()}
            className="px-3 py-1.5 bg-accent/10 hover:bg-accent/20 border border-accent/20 rounded font-medium text-accent transition-colors disabled:opacity-40 disabled:cursor-not-allowed text-xs"
          >
            {isCreatingSnapshot ? "Saving..." : "Snapshot"}
          </button>
        </div>
      </form>

      {/* Status messages */}
      {error && (
        <div className="mx-3 mt-3 p-2.5 bg-risk/10 border border-risk/20 text-risk rounded flex items-start gap-2 shrink-0">
          <ShieldAlert size={14} className="shrink-0 mt-0.5" />
          <span className="leading-normal">{error}</span>
        </div>
      )}
      {successMsg && (
        <div className="mx-3 mt-3 p-2.5 bg-accent2/10 border border-accent2/20 text-accent rounded flex items-start gap-2 shrink-0">
          <Check size={14} className="shrink-0 mt-0.5" />
          <span className="leading-normal">{successMsg}</span>
        </div>
      )}

      {/* Info Notice */}
      <div className="p-3 text-[11px] text-faint leading-relaxed border-b border-edge/30 bg-panel2/5 shrink-0">
        Workspace state is auto-snapshotted before agent edits (write_file and swarm patch application). Restores are fully undoable.
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {isLoading && checkpoints.length === 0 ? (
          <div className="flex items-center justify-center py-8 text-faint">
            Loading restore points...
          </div>
        ) : checkpoints.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-faint text-center gap-1">
            <span>No restore points available yet.</span>
            <span className="text-[10px]">Edits from the agent will create checkpoints here.</span>
          </div>
        ) : (
          checkpoints.map((cp) => {
            const isPending = isRestoring === cp.id;
            return (
              <div
                key={cp.id}
                className="p-2.5 bg-panel2 hover:bg-edge/20 border border-edge/60 rounded flex flex-col gap-1.5 transition-colors"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="font-medium text-txt break-words leading-snug flex-1">
                    {cp.label}
                  </div>
                  <span className="px-1.5 py-0.5 text-[9px] uppercase font-semibold tracking-wider bg-panel border border-edge/80 rounded text-faint shrink-0 select-none">
                    {formatTrigger(cp.trigger)}
                  </span>
                </div>
                
                <div className="flex items-center justify-between text-[10px] text-faint shrink-0">
                  <div className="font-mono">
                    {cp.id.slice(0, 8)}
                  </div>
                  <div>
                    {formatTime(cp.timestamp)}
                  </div>
                </div>

                <div className="flex gap-1.5 mt-1 border-t border-edge/30 pt-2 shrink-0">
                  <button
                    onClick={() => toggleDiff(cp.id)}
                    className="py-1 px-2.5 bg-panel border border-edge/80 hover:bg-edge/40 rounded font-medium text-muted hover:text-txt transition-colors text-[10px] flex items-center gap-1"
                  >
                    {expandedDiffs[cp.id] ? (
                      <>
                        <EyeOff size={10} />
                        <span>Hide diff</span>
                      </>
                    ) : (
                      <>
                        <Eye size={10} />
                        <span>View diff</span>
                      </>
                    )}
                  </button>

                  <button
                    onClick={() => handleRestore(cp)}
                    disabled={isRestoring !== null}
                    className="flex-1 py-1 px-2.5 bg-accent/5 hover:bg-accent/15 border border-accent/25 hover:border-accent/40 rounded font-medium text-accent hover:text-accent-bright transition-colors text-center text-[10px] flex items-center justify-center gap-1 disabled:opacity-40"
                  >
                    <Play size={10} className="fill-accent/20" />
                    {isPending ? "Restoring..." : "Restore to this point"}
                  </button>
                </div>

                {expandedDiffs[cp.id] && (
                  <div className="mt-2 border-t border-edge/30 pt-2 flex flex-col gap-2">
                    {loadingDiffs[cp.id] ? (
                      <div className="flex items-center gap-2 text-faint py-2 font-medium">
                        <RefreshCw size={10} className="animate-spin" />
                        <span>Fetching diff...</span>
                      </div>
                    ) : diffData[cp.id] ? (
                      (() => {
                        const diff = diffData[cp.id];
                        if (!diff.ok) {
                          return (
                            <div className="p-2 bg-risk/10 border border-risk/20 text-risk rounded text-[10px]">
                              {diff.error || "Failed to load diff."}
                            </div>
                          );
                        }

                        if (diff.files.length === 0) {
                          return (
                            <div className="text-faint py-1 italic text-[10.5px]">
                              No changes since this checkpoint
                            </div>
                          );
                        }

                        return (
                          <div className="flex flex-col gap-2 w-full overflow-hidden">
                            {/* Compact File List */}
                            <div className="flex flex-col gap-1 max-h-[120px] overflow-y-auto pr-1">
                              {diff.files.map((file, idx) => {
                                let badgeColor = "text-warn border-warn/30 bg-warn/5";
                                let label = "modified";
                                if (file.status === "added") {
                                  badgeColor = "text-good border-good/30 bg-good/5";
                                  label = "added";
                                } else if (file.status === "removed") {
                                  badgeColor = "text-risk border-risk/30 bg-risk/5";
                                  label = "removed";
                                }

                                return (
                                  <div key={idx} className="flex items-center justify-between gap-2 py-0.5 border-b border-edge/10 last:border-0">
                                    <span className="font-mono text-[10px] text-muted truncate max-w-[180px]" title={file.path}>
                                      {file.path}
                                    </span>
                                    <span className={`px-1 py-0.2 text-[8px] uppercase font-bold tracking-wider rounded border ${badgeColor}`}>
                                      {label}
                                    </span>
                                  </div>
                                );
                              })}
                            </div>

                            {/* Unified Diff Box */}
                            {diff.diff && (
                              <div className="flex flex-col gap-1">
                                <div className="text-[9px] uppercase tracking-wider text-faint font-semibold">
                                  Unified Diff
                                </div>
                                <div className="p-1.5 bg-panel border border-edge/80 rounded max-h-[180px] overflow-auto font-mono text-[10px] leading-relaxed text-muted scrollbar-thin">
                                  {diff.diff.split("\n").map((line, lineIdx) => {
                                    let lineClass = "text-muted/80";
                                    if (line.startsWith("+") && !line.startsWith("+++")) {
                                      lineClass = "text-good bg-good/5 border-l border-good/30 pl-1";
                                    } else if (line.startsWith("-") && !line.startsWith("---")) {
                                      lineClass = "text-risk bg-risk/5 border-l border-risk/30 pl-1";
                                    } else if (line.startsWith("@@")) {
                                      lineClass = "text-accent font-semibold pl-1";
                                    } else if (line.startsWith("diff") || line.startsWith("index") || line.startsWith("---") || line.startsWith("+++")) {
                                      lineClass = "text-faint select-none font-medium";
                                    }
                                    return (
                                      <div key={lineIdx} className={`whitespace-pre-wrap break-all min-h-[1.1rem] ${lineClass}`}>
                                        {line}
                                      </div>
                                    );
                                  })}
                                </div>
                                {diff.truncated && (
                                  <div className="text-[9px] text-warn italic">
                                    Diff truncated (size limit exceeded)
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })()
                    ) : null}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
