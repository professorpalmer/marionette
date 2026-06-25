import { useState, useEffect } from "react";
import { GitBranch, FileCode, RefreshCw, X } from "lucide-react";
import { nativeGit, isDesktop } from "../lib/transport";
import { api } from "../lib/api";

interface ChangedFile {
  status: string;
  path: string;
}

interface Branch {
  name: string;
  active: boolean;
}

export default function SourceControl() {
  const [repoPath, setRepoPath] = useState<string>(".");
  const [branches, setBranches] = useState<Branch[]>([]);
  const [changedFiles, setChangedFiles] = useState<ChangedFile[]>([]);
  
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Diff states
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [diffText, setDiffText] = useState<string | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  const loadGitStatus = async (path: string) => {
    setLoading(true);
    setError(null);
    try {
      const [statusRes, branchesRes] = await Promise.all([
        nativeGit.status(path),
        nativeGit.branches(path),
      ]);

      if (statusRes.ok) {
        setChangedFiles(statusRes.files || []);
      } else {
        setError(statusRes.error || "Failed to load git status");
      }

      if (branchesRes.ok) {
        setBranches(branchesRes.branches || []);
      }
    } catch (err: any) {
      setError(err.message || "Error running git operations");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!isDesktop) return;

    let active = true;
    async function init() {
      try {
        const cfg = await api.config();
        const path = cfg.repo || ".";
        if (!active) return;
        setRepoPath(path);
        await loadGitStatus(path);
      } catch (err: any) {
        if (active) setError(err.message || "Error getting config");
      }
    }
    init();
    return () => { active = false; };
  }, []);

  const handleFileClick = async (file: string) => {
    setSelectedFile(file);
    setDiffLoading(true);
    setDiffError(null);
    setDiffText(null);
    try {
      const res = await nativeGit.diff(repoPath, file);
      if (res.ok) {
        setDiffText(res.out || "No changes / empty diff");
      } else {
        setDiffError(res.error || "Failed to get diff");
      }
    } catch (err: any) {
      setDiffError(err.message || "Error generating diff");
    } finally {
      setDiffLoading(false);
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

  if (!isDesktop) {
    return (
      <div className="flex items-center justify-center h-full p-4 text-center bg-panel">
        <div className="text-[11px] text-muted uppercase tracking-wider">
          Source control requires the desktop app
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full overflow-hidden bg-panel">
      <div className="text-[10px] text-muted px-3 pt-2 uppercase tracking-wider flex items-center justify-between shrink-0">
        <span>Git Status</span>
        <button
          onClick={() => loadGitStatus(repoPath)}
          disabled={loading}
          className="text-muted hover:text-txt transition disabled:opacity-50"
          title="Refresh Git status"
        >
          <RefreshCw size={11} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 flex flex-col gap-3">
        {error && <div className="text-[11px] text-risk">{error}</div>}

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

        <div className="flex-1 flex flex-col min-h-[120px]">
          <div className="text-[9px] uppercase tracking-wider text-muted mb-1.5 font-semibold flex items-center justify-between">
            <span>Changed Files ({changedFiles.length})</span>
          </div>
          <div className="flex-1 overflow-y-auto border border-edge/30 rounded bg-panel2/30 flex flex-col divide-y divide-edge/20">
            {changedFiles.length === 0 && !loading && (
              <div className="text-[11px] text-muted italic p-3 text-center">
                No changes detected
              </div>
            )}
            {changedFiles.map((file) => {
              const style = getStatusStyle(file.status);
              return (
                <div
                  key={file.path}
                  onClick={() => handleFileClick(file.path)}
                  className={`flex items-center justify-between p-2 cursor-pointer transition hover:bg-panel2/60 ${
                    selectedFile === file.path ? "bg-panel2/80 text-accent" : "text-txt"
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    <FileCode size={12} className="text-muted shrink-0" />
                    <span className="text-[11px] truncate" title={file.path}>
                      {file.path}
                    </span>
                  </div>
                  <span
                    className={`text-[9px] font-mono font-semibold px-1 rounded border uppercase ${style.text}`}
                    title={style.label}
                  >
                    {file.status}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="h-1/2 border-t border-edge flex flex-col overflow-hidden bg-panel2 shrink-0">
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-edge bg-panel select-none shrink-0">
          <span className="text-[10px] text-muted uppercase tracking-wider truncate max-w-[80%]">
            {selectedFile ? `Diff: ${getFileName(selectedFile)}` : "No diff loaded"}
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
          {diffError && <div className="text-[11px] text-risk">{diffError}</div>}
          {!diffLoading && !diffError && diffText !== null && (
            <div className="space-y-0.5 select-text">
              {diffText.split("\n").map((line, idx) => renderDiffLine(line, idx))}
            </div>
          )}
          {!diffLoading && !diffError && diffText === null && (
            <div className="text-[11px] text-muted italic">
              Select a changed file above to view its diff
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
