import { useEffect, useId, useRef, useState } from "react";
import { GitFork, Plus, Trash2 } from "lucide-react";
import { api, type Worktree, type WorktreeCleanup } from "../lib/api";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (error && typeof error === "object" && "error" in error) return String(error.error);
  return "Worktree operation failed";
}

function cleanupStatus(message: string, cleanup?: WorktreeCleanup): string {
  if (!cleanup) return message;
  const removed = cleanup.count ? `; removed ${cleanup.count} clean worktree(s)` : "";
  const skipped = cleanup.skipped.length
    ? `; kept ${cleanup.skipped.length}: ${cleanup.skipped.map(item => `${item.path} (${item.reason})`).join("; ")}` : "";
  return message + removed + skipped;
}

export default function WorktreesPane() {
  const branchInputId = useId();
  const baseInputId = useId();
  const [worktrees, setWorktrees] = useState<Worktree[]>([]);
  const [maxWorktrees, setMaxWorktrees] = useState(25);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [repo, setRepo] = useState("");
  const epoch = useRef(0);
  const busy = useRef(false);
  const [newWtBranch, setNewWtBranch] = useState("");
  const [newWtBase, setNewWtBase] = useState("HEAD");
  const [wtError, setWtError] = useState("");
  const wtNotice = usePanelNotice(wtError || null);
  const [wtStatus, setWtStatus] = useState("");
  const disabled = loading || pending || !repo;

  const loadWorktrees = async (generation: number, requestedRepo?: string) => {
    setLoading(true);
    try {
      const data = await api.getWorktrees(requestedRepo);
      if (generation !== epoch.current) return;
      setWorktrees(data.worktrees);
      setMaxWorktrees(data.max);
      setRepo(data.repo);
    } catch (error) {
      if (generation !== epoch.current) return;
      setRepo("");
      setWtError(errorMessage(error));
    } finally {
      if (generation === epoch.current) setLoading(false);
    }
  };

  useEffect(() => {
    const refresh = (event?: Event) => {
      const generation = ++epoch.current;
      busy.current = false;
      setPending(false);
      setRepo("");
      setWorktrees([]);
      setWtError("");
      setWtStatus("");
      setNewWtBranch("");
      setNewWtBase("HEAD");
      const requested = event instanceof CustomEvent && event.type === "harness-project-selected"
        && typeof event.detail === "string" ? event.detail : undefined;
      void loadWorktrees(generation, requested);
    };
    refresh();
    window.addEventListener("harness-project-selected", refresh);
    window.addEventListener("harness-session-changed", refresh);
    return () => {
      epoch.current += 1;
      window.removeEventListener("harness-project-selected", refresh);
      window.removeEventListener("harness-session-changed", refresh);
    };
  }, []);

  const mutate = async <T,>(action: () => Promise<T>, status: string, success: string | ((result: T) => string),
    onSuccess?: () => void, onFailure?: () => void) => {
    if (disabled || busy.current) return;
    busy.current = true;
    setPending(true);
    const generation = epoch.current;
    setWtError("");
    setWtStatus(status);
    try {
      const result = await action();
      if (generation !== epoch.current) return;
      onSuccess?.();
      setWtStatus(typeof success === "string" ? success : success(result));
      await loadWorktrees(generation, repo);
    } catch (error) {
      if (generation !== epoch.current) return;
      onFailure?.();
      setWtError(errorMessage(error));
      setWtStatus("");
    } finally {
      if (generation === epoch.current) {
        busy.current = false;
        setPending(false);
      }
    }
  };

  const handleAddWorktree = () => {
    if (!newWtBranch.trim()) { setWtError("Branch name is required"); return; }
    void mutate(() => api.addWorktree(newWtBranch.trim(), newWtBase.trim() || undefined, repo),
      "Adding worktree...", result => cleanupStatus("Worktree added successfully", result.cleanup), () => {
        setNewWtBranch(""); setNewWtBase("HEAD");
      });
  };
  const handleRemoveWorktree = (path: string, branch: string) => {
    if (disabled || busy.current) return;
    if (window.confirm(`Remove worktree for branch "${branch}"?`)) {
      void mutate(() => api.removeWorktree(path, false, repo),
        "Removing worktree...", "Worktree removed successfully");
    }
  };
  const handlePruneWorktrees = () => {
    void mutate(() => api.pruneWorktrees(repo), "Pruning worktrees...", "Worktrees pruned successfully");
  };
  const handleMaxChange = (value: number) => {
    if (disabled || busy.current) return;
    const previous = maxWorktrees;
    setMaxWorktrees(value);
    void mutate(() => api.setWorktreeMax(value, repo), "Saving limit...", result => cleanupStatus("Limit saved", result.cleanup), undefined,
      () => setMaxWorktrees(previous));
  };

  return (
    <div className="flex flex-col h-full text-ui-12">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-edge">
        <span className="uppercase tracking-wider text-ui-10 text-faint font-medium flex items-center gap-1.5">
          <GitFork size={11} className="text-accent" /> Git Worktrees
        </span>
      </div>

      {/* Scrollable Container */}
      <div className="flex-1 overflow-y-auto p-2.5 flex flex-col gap-3">
        {/* Status messages */}
        {wtNotice && (
          <div role="alert" className="p-2 bg-risk/10 border border-risk/30 rounded text-risk text-ui-10.5 font-medium leading-relaxed">
            {wtNotice}
            <button disabled={loading || pending} onClick={() => {
              setWtError("");
              void loadWorktrees(++epoch.current);
            }} className="ml-2 underline">Retry</button>
          </div>
        )}
        {wtStatus && (
          <div role="status" className="p-2 bg-good/10 border border-good/30 rounded text-good text-ui-10.5 font-medium leading-relaxed">
            {wtStatus}
          </div>
        )}

        {/* Worktree List Section */}
        <div className="space-y-1.5">
          <div className="uppercase tracking-wider text-ui-9 text-faint font-semibold px-0.5">
            Active Worktrees ({worktrees.length})
          </div>
          
          <div className="space-y-1.5">
            {loading && worktrees.length === 0 ? (
              <div className="text-muted text-ui-11 text-center py-4 bg-panel2/15 border border-edge/35 rounded-lg">
                Loading worktrees...
              </div>
            ) : worktrees.length === 0 ? (
              <div className="text-muted text-ui-11 text-center py-6 bg-panel2/15 border border-edge/35 rounded-lg">
                {wtError ? "Worktrees unavailable." : "No active worktrees found."}
              </div>
            ) : (
              worktrees.map((wt) => (
                <div
                  key={wt.path}
                  className="p-2.5 bg-panel2/40 border border-edge rounded-lg text-ui-11 flex flex-col gap-1 hover:border-edge/70 transition-colors"
                >
                  <div className="flex items-center justify-between gap-2 min-w-0">
                    <span className="font-semibold text-txt flex items-center gap-1.5 min-w-0 flex-1 truncate">
                      <span className="truncate" title={wt.branch || "detached"}>{wt.branch || "detached"}</span>
                      {wt.is_main && (
                        <span className="bg-accent/15 text-accent text-ui-8.5 px-1 rounded font-bold uppercase tracking-wider flex-shrink-0">
                          main
                        </span>
                      )}
                      {wt.locked && (
                        <span className="bg-risk/15 text-risk text-ui-8.5 px-1 rounded font-bold uppercase tracking-wider flex-shrink-0">
                          locked
                        </span>
                      )}
                    </span>

                    {!wt.is_main && (
                      <button
                        disabled={disabled}
                        onClick={() => handleRemoveWorktree(wt.path, wt.branch)}
                        className="text-muted hover:text-risk transition-colors p-0.5 flex-shrink-0"
                        title="Remove worktree"
                      >
                        <Trash2 size={12} />
                      </button>
                    )}
                  </div>
                  
                  <div className="text-faint text-ui-9 font-mono truncate" title={wt.path}>
                    {wt.path}
                  </div>
                  
                  {wt.head && (
                    <div className="text-muted text-ui-9.5 font-mono bg-panel/35 px-1.5 py-0.5 rounded border border-edge/30 w-fit">
                      HEAD: {wt.head.slice(0, 7)}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>

        {/* Global actions: Prune & Max limit */}
        <div className="bg-panel2/20 border border-edge/50 rounded-lg p-2.5 flex flex-wrap items-center justify-between gap-2 text-ui-11">
          <button
            disabled={disabled}
            onClick={handlePruneWorktrees}
            className="bg-panel2 hover:bg-edge/40 border border-edge text-txt rounded px-2.5 py-1 font-medium transition-colors text-ui-10.5"
          >
            Prune Worktrees
          </button>

          <div className="flex items-center gap-2">
            <span className="text-faint uppercase text-ui-9 font-semibold">Max limit:</span>
            <input
              disabled={disabled}
              aria-label="Max worktrees"
              type="number"
              min="1"
              max="100"
              value={maxWorktrees}
              onChange={(e) => {
                const val = parseInt(e.target.value);
                if (!isNaN(val)) {
                  handleMaxChange(val);
                }
              }}
              className="w-12 bg-panel2 border border-edge rounded px-1.5 py-0.5 text-center font-mono focus:outline-none focus:border-accent"
            />
          </div>
        </div>

        <p className="text-faint">Adding a worktree or saving the global limit removes eligible clean worktrees, oldest first. Dirty, ignored-file, locked, active, and newly created trees are kept, so the count may exceed the limit.</p>

        {/* Add Worktree Section */}
        <div className="border-t border-edge/65 pt-3 mt-1.5 space-y-2">
          <div className="text-ui-9 uppercase tracking-wider text-faint font-semibold px-0.5">
            Add Worktree
          </div>
          <div className="space-y-2 bg-panel2/25 border border-edge/40 rounded-lg p-2.5">
            <div className="space-y-1">
              <label htmlFor={branchInputId} className="text-ui-9 uppercase tracking-wider text-faint font-medium">Branch name</label>
              <input
                id={branchInputId}
                disabled={disabled}
                type="text"
                placeholder="e.g., feature-x"
                value={newWtBranch}
                onChange={(e) => setNewWtBranch(e.target.value)}
                className="w-full bg-panel2 border border-edge rounded px-2.5 py-1.5 text-txt placeholder:text-faint text-ui-11 focus:outline-none focus:border-accent"
              />
            </div>
            
            <div className="space-y-1">
              <label htmlFor={baseInputId} className="text-ui-9 uppercase tracking-wider text-faint font-medium">Base commit-ish</label>
              <input
                id={baseInputId}
                disabled={disabled}
                type="text"
                placeholder="HEAD"
                value={newWtBase}
                onChange={(e) => setNewWtBase(e.target.value)}
                className="w-full bg-panel2 border border-edge rounded px-2.5 py-1.5 text-txt placeholder:text-faint text-ui-11 focus:outline-none focus:border-accent font-mono"
              />
            </div>

            <button
              disabled={disabled}
              onClick={handleAddWorktree}
              className="w-full bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded py-1.5 font-semibold text-ui-11 transition-colors flex items-center justify-center gap-1 mt-1"
            >
              <Plus size={12} /> Add Worktree
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
