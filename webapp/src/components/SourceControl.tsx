import { useState, useEffect, useCallback, useRef } from "react";
import { GitBranch, FileCode, RefreshCw, X, Plus, Minus } from "lucide-react";
import { nativeGit, gitWritesAvailable } from "../lib/transport";
import { api } from "../lib/api";
import { lastSelectedProjectRoot } from "../lib/panelTransition";
import { subscribeWorkspaceMutations } from "../lib/workspaceMutationEvents";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import { createGitRefreshCoordinator, type GitRefreshContext } from "../lib/gitRefreshCoordinator";
import { gitStatusPaintOnRepoChange, type GitStatusSnapshot } from "../lib/gitStatusPaint";

interface ChangedFile {
  status: string;
  path: string;
}

interface Branch {
  name: string;
  active: boolean;
}

export default function SourceControl() {
  const [branches, setBranches] = useState<Branch[]>([]);
  const [changedFiles, setChangedFiles] = useState<ChangedFile[]>([]);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const contextRef = useRef<GitRefreshContext | null>(null);
  const lastGoodByRepoRef = useRef<Map<string, GitStatusSnapshot>>(new Map());

  // Diff states
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [viewingStagedDiff, setViewingStagedDiff] = useState<boolean>(false);
  const [diffText, setDiffText] = useState<string | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  // Commit states
  const [commitMessage, setCommitMessage] = useState("");
  const [commitLoading, setCommitLoading] = useState(false);
  const [commitError, setCommitError] = useState<string | null>(null);
  const [commitStatus, setCommitStatus] = useState<string | null>(null);
  const errorNotice = usePanelNotice(error);
  const diffNotice = usePanelNotice(diffError);
  const commitNotice = usePanelNotice(commitError);
  const readOnly = !gitWritesAvailable();

  const paintRepoLists = useCallback((path: string) => {
    const paint = gitStatusPaintOnRepoChange(lastGoodByRepoRef.current.get(path));
    if (paint.kind === "keep" && paint.snapshot) {
      setChangedFiles(paint.snapshot.files);
      setBranches(paint.snapshot.branches);
      return;
    }
    setBranches([]);
    setChangedFiles([]);
  }, []);

  const resetRepoLocalChrome = useCallback(() => {
    setSelectedFile(null);
    setViewingStagedDiff(false);
    setDiffText(null);
    setDiffError(null);
    setDiffLoading(false);
    setCommitLoading(false);
    setError(null);
    setCommitMessage("");
    setCommitError(null);
    setCommitStatus(null);
  }, []);

  const [coordinator] = useState(() => {
    let reportedBranch: { epoch: number; branch: string } | null = null;
    const refresh = createGitRefreshCoordinator({
      onBusy: setLoading,
      onError: (error) => setError(error instanceof Error ? error.message : "Error running git operations"),
      run: async (lane, context): Promise<void> => {
        if (lane === "status") {
          setError(null);
          const result = await nativeGit.status(context.path);
          if (!refresh.isCurrent(context)) return;
          if (!result.ok) {
            setError(result.error || "Failed to load git status");
            return;
          }
          const files: ChangedFile[] = result.files || [];
          setChangedFiles(files);
          const previous = lastGoodByRepoRef.current.get(context.path);
          lastGoodByRepoRef.current.set(context.path, { files, branches: previous?.branches || [] });
          if (typeof result.branch === "string") {
            const changed = reportedBranch?.epoch === context.epoch && reportedBranch.branch !== result.branch;
            reportedBranch = { epoch: context.epoch, branch: result.branch };
            if (changed) void refresh.request(context, ["branches"]);
          }
        } else {
          const result = await nativeGit.branches(context.path);
          if (!refresh.isCurrent(context)) return;
          if (result.ok) {
            const nextBranches: Branch[] = result.branches || [];
            setBranches(nextBranches);
            const previous = lastGoodByRepoRef.current.get(context.path);
            lastGoodByRepoRef.current.set(context.path, { files: previous?.files || [], branches: nextBranches });
          }
        }
      },
    });
    return refresh;
  });

  const loadGitStatus = useCallback((context: GitRefreshContext | null, both = false) => {
    if (!context) return Promise.resolve();
    return coordinator.request(context, both ? ["status", "branches"] : ["status"]);
  }, [coordinator]);

  useEffect(() => {
    const activate = (path: string) => {
      const context = coordinator.activate(path);
      contextRef.current = context;
      resetRepoLocalChrome();
      paintRepoLists(path);
      return context;
    };
    const reloadFromConfig = async (context: GitRefreshContext) => {
      try {
        const cfg = await api.config();
        if (!coordinator.isCurrent(context)) return;
        const next = activate(cfg.repo || ".");
        await coordinator.request(next, ["status", "branches"]);
      } catch (error: unknown) {
        if (coordinator.isCurrent(context)) setError(error instanceof Error ? error.message : "Error getting config");
      }
    };
    void reloadFromConfig(activate(lastSelectedProjectRoot() || "."));
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;
    const onProject = (event: Event) => {
      if (!(event instanceof CustomEvent) || typeof event.detail !== "string") return;
      if (debounceTimer !== null) clearTimeout(debounceTimer);
      debounceTimer = null;
      void coordinator.request(activate(event.detail), ["status", "branches"]);
    };
    const onConfig = () => {
      if (debounceTimer !== null) clearTimeout(debounceTimer);
      const context = activate(contextRef.current?.path || ".");
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        void reloadFromConfig(context);
      }, 180);
    };
    const unsubMutations = subscribeWorkspaceMutations(() => {
      const context = contextRef.current;
      if (context) void coordinator.request(context, ["status"], "automatic");
    });
    window.addEventListener("harness-project-selected", onProject);
    window.addEventListener("harness-config-changed", onConfig);
    return () => {
      coordinator.deactivate();
      contextRef.current = null;
      if (debounceTimer !== null) clearTimeout(debounceTimer);
      window.removeEventListener("harness-project-selected", onProject);
      window.removeEventListener("harness-config-changed", onConfig);
      unsubMutations();
    };
  }, [coordinator, paintRepoLists, resetRepoLocalChrome]);

  const refreshDiff = async (file: string, isStaged: boolean) => {
    const context = contextRef.current;
    if (!context) return;
    setDiffLoading(true);
    setDiffError(null);
    setDiffText(null);
    try {
      const res = isStaged
        ? await nativeGit.diffStaged(context.path, file)
        : await nativeGit.diff(context.path, file);
      if (!coordinator.isCurrent(context)) return;
      if (res.ok) {
        setDiffText(res.out || "No changes / empty diff");
      } else {
        setDiffError(res.error || "Failed to get diff");
      }
    } catch (err: unknown) {
      if (!coordinator.isCurrent(context)) return;
      setDiffError(err instanceof Error ? err.message : "Error generating diff");
    } finally {
      if (coordinator.isCurrent(context)) setDiffLoading(false);
    }
  };

  const handleFileClick = async (file: string, isStaged: boolean) => {
    setSelectedFile(file);
    setViewingStagedDiff(isStaged);
    await refreshDiff(file, isStaged);
  };

  const handleStageFile = async (e: React.MouseEvent, file: string) => {
    const context = contextRef.current;
    if (!context) return;
    e.stopPropagation();
    setError(null);
    try {
      const res = await nativeGit.stageFile(context.path, file);
      if (!coordinator.isCurrent(context)) return;
      if (res.ok) {
        await loadGitStatus(context);
        if (!coordinator.isCurrent(context)) return;
        if (selectedFile === file) {
          await handleFileClick(file, true);
        }
      } else {
        setError(res.error || "Failed to stage file");
      }
    } catch (err: unknown) {
      if (!coordinator.isCurrent(context)) return;
      setError(err instanceof Error ? err.message : "Error staging file");
    }
  };

  const handleUnstageFile = async (e: React.MouseEvent, file: string) => {
    const context = contextRef.current;
    if (!context) return;
    e.stopPropagation();
    setError(null);
    try {
      const res = await nativeGit.unstageFile(context.path, file);
      if (!coordinator.isCurrent(context)) return;
      if (res.ok) {
        await loadGitStatus(context);
        if (!coordinator.isCurrent(context)) return;
        if (selectedFile === file) {
          await handleFileClick(file, false);
        }
      } else {
        setError(res.error || "Failed to unstage file");
      }
    } catch (err: unknown) {
      if (!coordinator.isCurrent(context)) return;
      setError(err instanceof Error ? err.message : "Error unstaging file");
    }
  };

  const handleStageAll = async () => {
    const context = contextRef.current;
    if (!context) return;
    setError(null);
    try {
      const res = await nativeGit.stageAll(context.path);
      if (!coordinator.isCurrent(context)) return;
      if (res.ok) {
        await loadGitStatus(context);
        if (!coordinator.isCurrent(context)) return;
        if (selectedFile) {
          await handleFileClick(selectedFile, true);
        }
      } else {
        setError(res.error || "Failed to stage all files");
      }
    } catch (err: unknown) {
      if (!coordinator.isCurrent(context)) return;
      setError(err instanceof Error ? err.message : "Error staging all files");
    }
  };

  const handleUnstageAll = async () => {
    const context = contextRef.current;
    if (!context) return;
    setError(null);
    try {
      const res = await nativeGit.unstageAll(context.path);
      if (!coordinator.isCurrent(context)) return;
      if (res.ok) {
        await loadGitStatus(context);
        if (!coordinator.isCurrent(context)) return;
        if (selectedFile) {
          await handleFileClick(selectedFile, false);
        }
      } else {
        setError(res.error || "Failed to unstage all files");
      }
    } catch (err: unknown) {
      if (!coordinator.isCurrent(context)) return;
      setError(err instanceof Error ? err.message : "Error unstaging all files");
    }
  };

  const handleCommit = async () => {
    const context = contextRef.current;
    if (!context) return;
    if (!commitMessage.trim()) return;
    setCommitLoading(true);
    setCommitError(null);
    setCommitStatus("Committing...");
    try {
      const res = await nativeGit.commit(context.path, commitMessage);
      if (!coordinator.isCurrent(context)) return;
      if (res.ok) {
        setCommitMessage("");
        setCommitStatus("Committed successfully");
        setSelectedFile(null);
        setDiffText(null);
        await loadGitStatus(context, true);
        if (!coordinator.isCurrent(context)) return;
        setTimeout(() => {
          if (coordinator.isCurrent(context)) setCommitStatus(null);
        }, 4000);
      } else {
        setCommitError(res.error || "Failed to commit");
        setCommitStatus(null);
      }
    } catch (err: unknown) {
      if (!coordinator.isCurrent(context)) return;
      setCommitError(err instanceof Error ? err.message : "Error running commit");
      setCommitStatus(null);
    } finally {
      if (coordinator.isCurrent(context)) setCommitLoading(false);
    }
  };

  const handleApplyHunk = async (hunk: { lines: string[] }, isStaged: boolean) => {
    const context = contextRef.current;
    if (!context) return;
    if (!selectedFile) return;
    setError(null);
    setDiffLoading(true);
    try {
      const lines = diffText ? diffText.split("\n") : [];
      const firstHunkIndex = lines.findIndex(l => l.startsWith("@@"));
      if (firstHunkIndex === -1) {
        setDiffError("Cannot reconstruct patch: no hunk index");
        setDiffLoading(false);
        return;
      }
      const headerText = lines.slice(0, firstHunkIndex).join("\n") + "\n";
      const hunkText = hunk.lines.join("\n") + "\n";
      const patch = headerText + hunkText;

      const reverse = isStaged;
      const res = await nativeGit.applyHunk(context.path, patch, reverse);
      if (!coordinator.isCurrent(context)) return;
      if (res.ok) {
        await loadGitStatus(context);
        if (!coordinator.isCurrent(context)) return;
        await refreshDiff(selectedFile, isStaged);
      } else {
        setDiffError(res.error || "Failed to apply hunk patch");
      }
    } catch (err: unknown) {
      if (!coordinator.isCurrent(context)) return;
      setDiffError(err instanceof Error ? err.message : "Error applying hunk patch");
    } finally {
      if (coordinator.isCurrent(context)) setDiffLoading(false);
    }
  };

  const getStatusStyle = (status: string) => {
    switch (status.trim()) {
      case "M":
        return { text: "text-warn border-warn/30 bg-warn/5", label: "modified" };
      case "A":
        return { text: "text-good border-good/30 bg-good/5", label: "added" };
      case "D":
        return { text: "text-risk border-risk/30 bg-risk/5", label: "deleted" };
      default:
        return { text: "text-muted border-edge bg-panel2", label: "untracked" };
    }
  };

  const renderDiffLine = (line: string, index: number) => {
    let className = "text-muted/80";
    if (line.startsWith("+") && !line.startsWith("+++")) {
      className = "text-good bg-good/5 border-l-2 border-good/30 pl-1";
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      className = "text-risk bg-risk/5 border-l-2 border-risk/30 pl-1";
    } else if (line.startsWith("@@")) {
      className = "text-accent font-semibold pl-1";
    } else if (line.startsWith("diff") || line.startsWith("index") || line.startsWith("---") || line.startsWith("+++")) {
      className = "text-muted select-none font-medium";
    }

    return (
      <div key={index} className={`whitespace-pre font-mono text-[11px] min-h-[1.2rem] ${className}`}>
        {line}
      </div>
    );
  };

  const getFileName = (pathStr: string) => {
    const parts = pathStr.split(/[/\\]/);
    return parts[parts.length - 1] || pathStr;
  };

  const stagedFiles: ChangedFile[] = [];
  const unstagedFiles: ChangedFile[] = [];

  changedFiles.forEach((file) => {
    const x: any = file.status.charAt(0);
    const y: any = file.status.charAt(1);

    // Staged: X is not space, and not '?' (untracked), and not empty
    if (x !== " " && x !== "?" && x !== "") {
      stagedFiles.push({
        path: file.path,
        status: x,
      });
    }

    // Unstaged/Changes: Y is not space and not empty, or it's untracked ("??")
    if ((y !== " " && y !== "") || (x === "?" && y === "?")) {
      unstagedFiles.push({
        path: file.path,
        status: (x === "?" && y === "?") ? "??" : y,
      });
    }
  });

  // Hunk parser
  let headerLines: string[] = [];
  let hunks: { header: string; lines: string[] }[] = [];
  let hasHunks = false;

  if (diffText) {
    const lines = diffText.split("\n");
    const firstHunkIndex = lines.findIndex((l) => l.startsWith("@@"));
    if (firstHunkIndex !== -1) {
      hasHunks = true;
      headerLines = lines.slice(0, firstHunkIndex);
      let currentHunk: { header: string; lines: string[] } | null = null;
      for (let i = firstHunkIndex; i < lines.length; i++) {
        const line = lines[i];
        if (line.startsWith("@@")) {
          if (currentHunk) {
            hunks.push(currentHunk);
          }
          currentHunk = {
            header: line,
            lines: [line],
          };
        } else if (currentHunk) {
          currentHunk.lines.push(line);
        }
      }
      if (currentHunk) {
        hunks.push(currentHunk);
      }
    }
  }

  return (
    <div className="flex flex-col h-full overflow-hidden bg-transparent">
      {readOnly && (
        <div className="text-[10px] text-muted px-3 py-1.5 border-b border-edge/30 bg-panel2/40 uppercase tracking-wider">
          Read-only — stage and commit require the desktop app
        </div>
      )}
      <div className="text-[10px] text-muted px-3 pt-2 uppercase tracking-wider flex items-center justify-between shrink-0">
        <span>Git Status</span>
        <button
          onClick={() => loadGitStatus(contextRef.current, true)}
          disabled={loading}
          className="text-muted hover:text-txt transition disabled:opacity-50"
          title="Refresh Git status"
        >
          <RefreshCw size={11} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 flex flex-col gap-3">
        {errorNotice && <div className="text-[11px] text-risk">{errorNotice}</div>}

        <div>
          <div className="text-[9px] uppercase tracking-wider text-muted mb-1.5 font-semibold">
            Branches
          </div>
          <div className="flex flex-wrap gap-1 max-h-[80px] overflow-y-auto border border-edge/30 rounded p-1.5 bg-panel2/50">
            {branches.length === 0 && !loading && (
              <div className="text-[10px] text-muted italic">No branches found</div>
            )}
            {branches.map((b) => (
              <span
                key={b.name}
                className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] border ${
                  b.active
                    ? "text-accent border-accent/40 bg-accent/5 font-semibold"
                    : "text-muted border-edge hover:text-txt"
                }`}
              >
                <GitBranch size={10} />
                {b.name}
              </span>
            ))}
          </div>
        </div>

        <div className="flex-1 flex flex-col min-h-[120px] overflow-y-auto gap-4 pr-1">
          {/* Staged Group */}
          <div className="flex flex-col">
            <div className="text-[9px] uppercase tracking-wider text-muted mb-1.5 font-semibold flex items-center justify-between">
              <span>Staged ({stagedFiles.length})</span>
              {stagedFiles.length > 0 && !readOnly && (
                <button
                  onClick={handleUnstageAll}
                  className="text-[9px] text-muted hover:text-accent font-medium uppercase tracking-wider transition"
                >
                  Unstage all
                </button>
              )}
            </div>
            <div className="border border-edge/30 rounded bg-panel2/30 flex flex-col divide-y divide-edge/20 max-h-[160px] overflow-y-auto">
              {stagedFiles.length === 0 && (
                <div className="text-[10px] text-muted italic p-2 text-center select-none">
                  No staged changes
                </div>
              )}
              {stagedFiles.map((file) => {
                const style = getStatusStyle(file.status);
                const isSelected = selectedFile === file.path && viewingStagedDiff;
                return (
                  <div
                    key={`staged-${file.path}`}
                    onClick={() => handleFileClick(file.path, true)}
                    className={`flex items-center justify-between p-2 cursor-pointer transition hover:bg-panel2/60 group ${
                      isSelected ? "bg-panel2/80 text-accent" : "text-txt"
                    }`}
                  >
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      <FileCode size={12} className="text-muted shrink-0" />
                      <span className="text-[11px] truncate" title={file.path}>
                        {file.path}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      {!readOnly && (
                      <button
                        onClick={(e) => handleUnstageFile(e, file.path)}
                        className="opacity-0 group-hover:opacity-100 transition p-0.5 hover:bg-panel3 border border-edge/30 rounded text-muted hover:text-risk"
                        title="Unstage file"
                      >
                        <Minus size={10} />
                      </button>
                      )}
                      <span
                        className={`text-[9px] font-mono font-semibold px-1 rounded border uppercase ${style.text}`}
                        title={style.label}
                      >
                        {file.status}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Unstaged Group */}
          <div className="flex flex-col">
            <div className="text-[9px] uppercase tracking-wider text-muted mb-1.5 font-semibold flex items-center justify-between">
              <span>Changes ({unstagedFiles.length})</span>
              {unstagedFiles.length > 0 && !readOnly && (
                <button
                  onClick={handleStageAll}
                  className="text-[9px] text-muted hover:text-accent font-medium uppercase tracking-wider transition"
                >
                  Stage all
                </button>
              )}
            </div>
            <div className="border border-edge/30 rounded bg-panel2/30 flex flex-col divide-y divide-edge/20 max-h-[160px] overflow-y-auto">
              {unstagedFiles.length === 0 && (
                <div className="text-[10px] text-muted italic p-2 text-center select-none">
                  No unstaged changes
                </div>
              )}
              {unstagedFiles.map((file) => {
                const style = getStatusStyle(file.status);
                const isSelected = selectedFile === file.path && !viewingStagedDiff;
                return (
                  <div
                    key={`unstaged-${file.path}`}
                    onClick={() => handleFileClick(file.path, false)}
                    className={`flex items-center justify-between p-2 cursor-pointer transition hover:bg-panel2/60 group ${
                      isSelected ? "bg-panel2/80 text-accent" : "text-txt"
                    }`}
                  >
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      <FileCode size={12} className="text-muted shrink-0" />
                      <span className="text-[11px] truncate" title={file.path}>
                        {file.path}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      {!readOnly && (
                      <button
                        onClick={(e) => handleStageFile(e, file.path)}
                        className="opacity-0 group-hover:opacity-100 transition p-0.5 hover:bg-panel3 border border-edge/30 rounded text-muted hover:text-good"
                        title="Stage file"
                      >
                        <Plus size={10} />
                      </button>
                      )}
                      <span
                        className={`text-[9px] font-mono font-semibold px-1 rounded border uppercase ${style.text}`}
                        title={style.label}
                      >
                        {file.status}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      {!readOnly && (
      <div className="border-t border-edge/30 p-3 bg-panel/50 flex flex-col gap-2 shrink-0">
        <textarea
          value={commitMessage}
          onChange={(e) => setCommitMessage(e.target.value)}
          placeholder="Commit message... (no emojis)"
          rows={2}
          className="w-full text-[11px] bg-bg border border-edge/40 rounded p-1.5 focus:outline-none focus:border-accent/50 resize-none text-txt placeholder:text-muted"
        />
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-muted truncate max-w-[60%]">
            {commitStatus || (stagedFiles.length > 0 ? `${stagedFiles.length} files staged` : "Nothing staged")}
          </span>
          <button
            onClick={handleCommit}
            disabled={!commitMessage.trim() || stagedFiles.length === 0 || commitLoading}
            className="px-3 py-1 bg-panel2 border border-edge hover:border-accent/40 hover:text-accent disabled:opacity-50 disabled:hover:text-muted disabled:hover:border-edge rounded text-[11px] font-medium text-txt transition flex items-center gap-1 shrink-0"
          >
            {commitLoading ? "Committing..." : "Commit"}
          </button>
        </div>
        {commitNotice && <div className="text-[10px] text-risk mt-1">{commitNotice}</div>}
      </div>
      )}

      <div className="h-1/2 border-t border-edge flex flex-col overflow-hidden bg-panel2 shrink-0">
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-edge bg-panel select-none shrink-0">
          <span className="text-[10px] text-muted uppercase tracking-wider truncate max-w-[80%]">
            {selectedFile ? `Diff: ${getFileName(selectedFile)} ${viewingStagedDiff ? "(staged)" : "(unstaged)"}` : "No diff loaded"}
          </span>
          {selectedFile && (
            <button
              onClick={() => {
                setSelectedFile(null);
                setDiffText(null);
                setDiffError(null);
              }}
              className="text-muted hover:text-txt transition"
              title="Clear diff view"
            >
              <X size={12} />
            </button>
          )}
        </div>
        <div className="flex-1 overflow-auto bg-bg p-3">
          {diffLoading && (
            <div className="text-[11px] text-muted">Generating diff view...</div>
          )}
          {diffNotice && <div className="text-[11px] text-risk">{diffNotice}</div>}
          
          {!diffLoading && !diffNotice && diffText !== null && !hasHunks && (
            <div className="space-y-0.5 select-text">
              {diffText.split("\n").map((line, idx) => renderDiffLine(line, idx))}
            </div>
          )}

          {!diffLoading && !diffNotice && diffText !== null && hasHunks && (
            <div className="space-y-4">
              {headerLines.length > 0 && (
                <div className="p-1.5 bg-panel2/30 border border-edge/10 rounded text-muted font-mono text-[10px] select-text">
                  {headerLines.map((line, idx) => (
                    <div key={idx} className="truncate">{line}</div>
                  ))}
                </div>
              )}

              {hunks.map((hunk, hunkIdx) => (
                <div key={hunkIdx} className="border border-edge/20 rounded overflow-hidden bg-bg/50 group/hunk">
                  <div className="flex items-center justify-between px-2 py-1 bg-panel border-b border-edge/20 select-none">
                    <span className="text-[10px] font-mono text-accent font-semibold">
                      {hunk.header}
                    </span>
                    {!readOnly && (
                    <button
                      onClick={() => handleApplyHunk(hunk, viewingStagedDiff)}
                      className="opacity-0 group-hover/hunk:opacity-100 transition px-2 py-0.5 bg-panel2 border border-edge/60 hover:border-accent/40 rounded text-[9px] text-muted hover:text-accent font-medium"
                    >
                      {viewingStagedDiff ? "Unstage hunk" : "Stage hunk"}
                    </button>
                    )}
                  </div>
                  <div className="p-2 space-y-0.5">
                    {hunk.lines.slice(1).map((line, idx) => renderDiffLine(line, idx))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {!diffLoading && !diffNotice && diffText === null && (
            <div className="text-[11px] text-muted italic">
              Select a changed file above to view its diff
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
