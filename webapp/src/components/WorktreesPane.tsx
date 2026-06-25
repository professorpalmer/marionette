import { useEffect, useState } from "react";
import { GitFork, Plus, Trash2 } from "lucide-react";
import { api, type Worktree } from "../lib/api";

export default function WorktreesPane() {
  const [worktrees, setWorktrees] = useState<Worktree[]>([]);
  const [maxWorktrees, setMaxWorktrees] = useState<number>(25);
  const [loading, setLoading] = useState(true);

  // Form states for worktrees
  const [newWtBranch, setNewWtBranch] = useState("");
  const [newWtBase, setNewWtBase] = useState("HEAD");
  const [wtError, setWtError] = useState("");
  const [wtStatus, setWtStatus] = useState("");

  const loadWorktrees = async () => {
    try {
      setLoading(true);
      const data = await api.getWorktrees();
      setWorktrees(data.worktrees || []);
      setMaxWorktrees(data.max ?? 25);
    } catch (err) {
      console.error("Failed to load worktrees", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadWorktrees();
  }, []);

  const handleAddWorktree = async () => {
    if (!newWtBranch.trim()) {
      setWtError("Branch name is required");
      return;
    }
    try {
      setWtError("");
      setWtStatus("Adding worktree...");
      await api.addWorktree(newWtBranch.trim(), newWtBase.trim() || undefined);
      setWtStatus("Worktree added successfully");
      setNewWtBranch("");
      setNewWtBase("HEAD");
      setTimeout(() => setWtStatus(""), 3000);
      loadWorktrees();
    } catch (err: any) {
      setWtError(err?.error || "Failed to add worktree");
      setWtStatus("");
    }
  };

  const handleRemoveWorktree = async (path: string, branch: string) => {
    if (window.confirm(`Are you sure you want to remove worktree for branch "${branch}"?`)) {
      try {
        setWtError("");
        setWtStatus("Removing worktree...");
        const res = await api.removeWorktree(path);
        if (res.ok) {
          setWtStatus("Worktree removed successfully");
          setTimeout(() => setWtStatus(""), 3000);
          loadWorktrees();
        }
      } catch (err: any) {
        setWtError(err?.error || "Failed to remove worktree");
        setWtStatus("");
      }
    }
  };

  const handlePruneWorktrees = async () => {
    try {
      setWtError("");
      setWtStatus("Pruning worktrees...");
      const res = await api.pruneWorktrees();
      if (res.ok) {
        setWtStatus("Worktrees pruned successfully");
        setTimeout(() => setWtStatus(""), 3000);
        loadWorktrees();
      }
    } catch (err: any) {
      setWtError(err?.error || "Failed to prune worktrees");
      setWtStatus("");
    }
  };

  const handleMaxChange = async (val: number) => {
    setMaxWorktrees(val);
    try {
      await api.setWorktreeMax(val);
    } catch (err) {
      console.error("Failed to update max worktrees", err);
    }
  };

  return (
    <div className="flex flex-col h-full text-[12px]">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-edge">
        <span className="uppercase tracking-wider text-[10px] text-faint font-medium flex items-center gap-1.5">
          <GitFork size={11} className="text-accent" /> Git Worktrees
        </span>
      </div>

      {/* Scrollable Container */}
      <div className="flex-1 overflow-y-auto p-2.5 flex flex-col gap-3">
        {/* Status messages */}
        {wtError && (
          <div className="p-2 bg-risk/10 border border-risk/30 rounded text-risk text-[10.5px] font-medium leading-relaxed">
            {wtError}
          </div>
        )}
        {wtStatus && (
          <div className="p-2 bg-good/10 border border-good/30 rounded text-good text-[10.5px] font-medium leading-relaxed">
            {wtStatus}
          </div>
        )}

        {/* Worktree List Section */}
        <div className="space-y-1.5">
          <div className="uppercase tracking-wider text-[9px] text-faint font-semibold px-0.5">
            Active Worktrees ({worktrees.length})
          </div>
          
          <div className="space-y-1.5">
            {loading && worktrees.length === 0 ? (
              <div className="text-muted text-[11px] text-center py-4 bg-panel2/15 border border-edge/35 rounded-lg">
                Loading worktrees...
              </div>
            ) : worktrees.length === 0 ? (
              <div className="text-muted text-[11px] text-center py-6 bg-panel2/15 border border-edge/35 rounded-lg">
                No active worktrees found.
              </div>
            ) : (
              worktrees.map((wt) => (
                <div
                  key={wt.path}
                  className="p-2.5 bg-panel2/40 border border-edge rounded-lg text-[11px] flex flex-col gap-1 hover:border-edge/70 transition-colors"
                >
                  <div className="flex items-center justify-between gap-2 min-w-0">
                    <span className="font-semibold text-txt flex items-center gap-1.5 min-w-0 flex-1 truncate">
                      <span className="truncate" title={wt.branch || "detached"}>{wt.branch || "detached"}</span>
                      {wt.is_main && (
                        <span className="bg-accent/15 text-accent text-[8.5px] px-1 rounded font-bold uppercase tracking-wider flex-shrink-0">
                          main
                        </span>
                      )}
                      {wt.locked && (
                        <span className="bg-risk/15 text-risk text-[8.5px] px-1 rounded font-bold uppercase tracking-wider flex-shrink-0">
                          locked
                        </span>
                      )}
                    </span>

                    {!wt.is_main && (
                      <button
                        onClick={() => handleRemoveWorktree(wt.path, wt.branch)}
                        className="text-muted hover:text-risk transition-colors p-0.5 flex-shrink-0"
                        title="Remove worktree"
                      >
                        <Trash2 size={12} />
                      </button>
                    )}
                  </div>
                  
                  <div className="text-faint text-[9px] font-mono truncate" title={wt.path}>
                    {wt.path}
                  </div>
                  
                  {wt.head && (
                    <div className="text-muted text-[9.5px] font-mono bg-panel/35 px-1.5 py-0.5 rounded border border-edge/30 w-fit">
                      HEAD: {wt.head.slice(0, 7)}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>

        {/* Global actions: Prune & Max limit */}
        <div className="bg-panel2/20 border border-edge/50 rounded-lg p-2.5 flex flex-wrap items-center justify-between gap-2 text-[11px]">
          <button
            onClick={handlePruneWorktrees}
            className="bg-panel2 hover:bg-edge/40 border border-edge text-txt rounded px-2.5 py-1 font-medium transition-colors text-[10.5px]"
          >
            Prune Worktrees
          </button>

          <div className="flex items-center gap-2">
            <span className="text-faint uppercase text-[9px] font-semibold">Max limit:</span>
            <input
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

        {/* Add Worktree Section */}
        <div className="border-t border-edge/65 pt-3 mt-1.5 space-y-2">
          <div className="text-[9px] uppercase tracking-wider text-faint font-semibold px-0.5">
            Add Worktree
          </div>
          <div className="space-y-2 bg-panel2/25 border border-edge/40 rounded-lg p-2.5">
            <div className="space-y-1">
              <label className="text-[9px] uppercase tracking-wider text-faint font-medium">Branch name</label>
              <input
                type="text"
                placeholder="e.g., feature-x"
                value={newWtBranch}
                onChange={(e) => setNewWtBranch(e.target.value)}
                className="w-full bg-panel2 border border-edge rounded px-2.5 py-1.5 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent"
              />
            </div>
            
            <div className="space-y-1">
              <label className="text-[9px] uppercase tracking-wider text-faint font-medium">Base commit-ish</label>
              <input
                type="text"
                placeholder="HEAD"
                value={newWtBase}
                onChange={(e) => setNewWtBase(e.target.value)}
                className="w-full bg-panel2 border border-edge rounded px-2.5 py-1.5 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent font-mono"
              />
            </div>

            <button
              onClick={handleAddWorktree}
              className="w-full bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded py-1.5 font-semibold text-[11px] transition-colors flex items-center justify-center gap-1 mt-1"
            >
              <Plus size={12} /> Add Worktree
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
