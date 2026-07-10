import { useState, useEffect, useCallback, useRef } from "react";
import { History, Play, ShieldAlert, Check, RefreshCw, Eye, EyeOff } from "lucide-react";
import { api, type Checkpoint, type CheckpointDiff } from "../lib/api";
import { lastSelectedProjectRoot } from "../lib/panelTransition";

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

  // Scope key: repo + active session — never leave another project's list painted.
  const [projectRoot, setProjectRoot] = useState(lastSelectedProjectRoot);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const scopeKey = `${projectRoot || "__none__"}::${activeSessionId || "__none__"}`;
  const fetchGenRef = useRef(0);

  const clearLocalState = useCallback(() => {
    setCheckpoints([]);
    setExpandedDiffs({});
    setDiffData({});
    setLoadingDiffs({});
    setError(null);
    setSuccessMsg(null);
    setIsRestoring(null);
  }, []);

  const fetchCheckpoints = useCallback(async () => {
    const gen = ++fetchGenRef.current;
    setIsLoading(true);
    setError(null);
    try {
      const list = await api.getCheckpoints();
      if (gen !== fetchGenRef.current) return;
      // Sort newest first
      const sorted = [...list].sort((a, b) => b.timestamp - a.timestamp);
      setCheckpoints(sorted);
    } catch (err: any) {
      if (gen !== fetchGenRef.current) return;
      const raw = err?.message || "Failed to fetch checkpoints";
      // Soften the common boot/respawn race (backend briefly not listening).
      const soft = /ECONNREFUSED|ECONNRESET|socket hang up/i.test(raw)
        ? "Harness is starting up — retrying…"
        : raw;
      setError(soft);
    } finally {
      if (gen === fetchGenRef.current) setIsLoading(false);
    }
  }, []);

  const refreshScope = useCallback(async () => {
    try {
      const ws = await api.getWorkspace();
      const repo = ws?.repo || "";
      setProjectRoot(repo);
      if (!repo) {
        setActiveSessionId(null);
        return;
      }
      const sessions = await api.sessions(repo);
      const active = sessions.find((s) => s.active);
      setActiveSessionId(active?.id ?? null);
    } catch {
      // Keep last known scope; fetch may still succeed against server active.
    }
  }, []);

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

  // Clear + refetch whenever project/session scope changes.
  useEffect(() => {
    clearLocalState();
    fetchCheckpoints();
  }, [scopeKey, clearLocalState, fetchCheckpoints]);

  useEffect(() => {
    void refreshScope();

    const onProject = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (typeof path === "string") {
        // Clear immediately so the previous project's list never lingers.
        clearLocalState();
        setProjectRoot(path);
      }
      void refreshScope();
    };
    const onSessionOrConfig = () => {
      clearLocalState();
      void refreshScope();
    };
    const onMutated = () => fetchCheckpoints();
    const onVisible = () => { if (!document.hidden) fetchCheckpoints(); };
    // Electron: main fires this after an unexpected backend respawn on a new
    // port. Re-fetch so a transient ECONNREFUSED doesn't stick in the panel.
    const ipc: any = (typeof window !== "undefined" && (window as any).harnessIPC) || null;
    const unsubRespawn = typeof ipc?.onBackendRespawned === "function"
      ? ipc.onBackendRespawned(() => { void fetchCheckpoints(); })
      : null;

    window.addEventListener("harness-project-selected", onProject);
    window.addEventListener("harness-config-changed", onSessionOrConfig);
    window.addEventListener("harness-new-session", onSessionOrConfig);
    window.addEventListener("harness-repo-mutated", onMutated);
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("harness-project-selected", onProject);
      window.removeEventListener("harness-config-changed", onSessionOrConfig);
      window.removeEventListener("harness-new-session", onSessionOrConfig);
      window.removeEventListener("harness-repo-mutated", onMutated);
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
      try { unsubRespawn?.(); } catch { /* ignore */ }
    };
  }, [clearLocalState, fetchCheckpoints, refreshScope]);

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
    <div className="flex flex-col h-full min-h-0 bg-panel text-txt text-xs">
      {/* Header + inline manual-snapshot: one compact row to save vertical space
          in a split pane (was a separate header + form section). */}
      <div className="px-2 py-1.5 border-b border-edge flex items-center gap-1.5 bg-panel2/15 shrink-0">
        <History size={12} className="text-accent shrink-0" />
        <form onSubmit={handleCreateSnapshot} className="flex items-center gap-1 flex-1 min-w-0">
          <input
            type="text"
            placeholder="Snapshot label..."
            value={snapshotLabel}
            onChange={(e) => setSnapshotLabel(e.target.value)}
            disabled={isCreatingSnapshot}
            title="Auto-snapshots are taken before agent edits and swarm patches. Restores are fully undoable."
            className="flex-1 min-w-0 px-1.5 py-0.5 bg-panel2 border border-edge rounded text-txt placeholder-faint focus:outline-none focus:border-accent/50 text-[11px]"
          />
          <button
            type="submit"
            disabled={isCreatingSnapshot || !snapshotLabel.trim()}
            className="px-2 py-0.5 bg-accent/10 hover:bg-accent/20 border border-accent/20 rounded font-medium text-accent transition-colors disabled:opacity-40 disabled:cursor-not-allowed text-[11px] shrink-0"
          >
            {isCreatingSnapshot ? "..." : "Snap"}
          </button>
        </form>
        <button
          onClick={fetchCheckpoints}
          disabled={isLoading}
          title="Refresh checkpoints"
          className="p-0.5 hover:bg-edge/50 rounded text-faint hover:text-muted transition-colors shrink-0"
        >
          <RefreshCw size={11} className={isLoading ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Status messages */}
      {error && (
        <div className="mx-2 mt-1.5 p-1.5 bg-risk/10 border border-risk/20 text-risk rounded flex items-start gap-1.5 shrink-0 text-[10px]">
          <ShieldAlert size={12} className="shrink-0 mt-0.5" />
          <span className="leading-snug">{error}</span>
        </div>
      )}
      {successMsg && (
        <div className="mx-2 mt-1.5 p-1.5 bg-accent2/10 border border-accent2/20 text-accent rounded flex items-start gap-1.5 shrink-0 text-[10px]">
          <Check size={12} className="shrink-0 mt-0.5" />
          <span className="leading-snug">{successMsg}</span>
        </div>
      )}

      {/* List */}
      <div className="flex-1 min-h-0 overflow-y-auto p-1.5 space-y-1">
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
                className="px-1.5 py-1 bg-panel2 hover:bg-edge/20 border border-edge/60 rounded flex flex-col gap-0.5 transition-colors"
              >
                <div className="flex items-center justify-between gap-1.5 min-w-0">
                  <div className="font-medium text-txt truncate leading-snug flex-1 min-w-0 text-[11px]" title={cp.label}>
                    {cp.label}
                  </div>
                  <span className="px-1 py-px text-[8px] uppercase font-semibold tracking-wide bg-panel border border-edge/80 rounded text-faint shrink-0 select-none">
                    {formatTrigger(cp.trigger)}
                  </span>
                </div>

                <div className="flex items-center justify-between text-[9px] text-faint shrink-0">
                  <span className="font-mono">{cp.id.slice(0, 8)}</span>
                  <span>{formatTime(cp.timestamp)}</span>
                </div>

                <div className="flex gap-1 shrink-0">
                  <button
                    onClick={() => toggleDiff(cp.id)}
                    title={expandedDiffs[cp.id] ? "Hide diff" : "View diff"}
                    className="py-0.5 px-1.5 bg-panel border border-edge/80 hover:bg-edge/40 rounded font-medium text-muted hover:text-txt transition-colors text-[10px] flex items-center gap-1 shrink-0"
                  >
                    {expandedDiffs[cp.id] ? <EyeOff size={10} /> : <Eye size={10} />}
                    <span>Diff</span>
                  </button>

                  <button
                    onClick={() => handleRestore(cp)}
                    disabled={isRestoring !== null}
                    className="flex-1 py-0.5 px-1.5 bg-accent/5 hover:bg-accent/15 border border-accent/25 hover:border-accent/40 rounded font-medium text-accent hover:text-accent-bright transition-colors text-center text-[10px] flex items-center justify-center gap-1 disabled:opacity-40"
                  >
                    <Play size={10} className="fill-accent/20" />
                    {isPending ? "Restoring..." : "Restore"}
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
