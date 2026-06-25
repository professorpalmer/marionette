import { useState, useEffect } from "react";
import { ChevronRight, ChevronDown, File, X } from "lucide-react";
import { nativeFs, isDesktop } from "../lib/transport";
import { api } from "../lib/api";

interface TreeNodeProps {
  name: string;
  path: string;
  isDir: boolean;
  onFileSelect: (path: string) => void;
  selectedPath: string | null;
}

function TreeNode({ name, path, isDir, onFileSelect, selectedPath }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(false);
  const [children, setChildren] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleExpand = async () => {
    if (!isDir) {
      onFileSelect(path);
      return;
    }

    const nextExpanded = !expanded;
    setExpanded(nextExpanded);

    if (nextExpanded && children.length === 0) {
      setLoading(true);
      setError(null);
      try {
        const res = await nativeFs.readDir(path);
        if (res.ok && res.nodes) {
          const sorted = [...res.nodes].sort((a, b) => {
            if (a.dir !== b.dir) return a.dir ? -1 : 1;
            return a.name.localeCompare(b.name);
          });
          setChildren(sorted);
        } else {
          setError(res.error || "Failed to load directory");
        }
      } catch (err: any) {
        setError(err.message || "Error reading folder");
      } finally {
        setLoading(false);
      }
    }
  };

  return (
    <div className="select-none">
      <div
        onClick={toggleExpand}
        className={`flex items-center gap-1.5 py-1 px-1.5 rounded cursor-pointer text-[12px] hover:bg-panel2/80 transition ${
          selectedPath === path ? "bg-panel2 text-accent" : "text-txt"
        }`}
      >
        {isDir ? (
          <>
            {expanded ? (
              <ChevronDown size={14} className="text-muted shrink-0" />
            ) : (
              <ChevronRight size={14} className="text-muted shrink-0" />
            )}
            <span className="truncate font-medium">{name}</span>
          </>
        ) : (
          <>
            <File size={14} className="text-muted shrink-0 ml-[14px]" />
            <span className="truncate">{name}</span>
          </>
        )}
      </div>

      {isDir && expanded && (
        <div className="pl-3 border-l border-edge/40 ml-2.5 mt-0.5 mb-1 flex flex-col gap-0.5">
          {loading && <div className="text-[11px] text-muted pl-2 py-0.5">Loading...</div>}
          {error && <div className="text-[11px] text-risk pl-2 py-0.5">{error}</div>}
          {!loading && !error && children.length === 0 && (
            <div className="text-[11px] text-muted italic pl-2 py-0.5">Empty directory</div>
          )}
          {children.map((child) => (
            <TreeNode
              key={child.path}
              name={child.name}
              path={child.path}
              isDir={child.dir}
              onFileSelect={onFileSelect}
              selectedPath={selectedPath}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function FileTree() {
  const [repoPath, setRepoPath] = useState<string>(".");
  const [rootNodes, setRootNodes] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);

  useEffect(() => {
    if (!isDesktop) return;

    let active = true;
    async function init() {
      setLoading(true);
      setError(null);
      try {
        const cfg = await api.config();
        const path = cfg.repo || ".";
        if (!active) return;
        setRepoPath(path);

        const res = await nativeFs.readDir(path);
        if (!active) return;
        if (res.ok && res.nodes) {
          const sorted = [...res.nodes].sort((a, b) => {
            if (a.dir !== b.dir) return a.dir ? -1 : 1;
            return a.name.localeCompare(b.name);
          });
          setRootNodes(sorted);
        } else {
          setError(res.error || "Failed to load directory");
        }
      } catch (err: any) {
        if (active) setError(err.message || "Error starting file tree");
      } finally {
        if (active) setLoading(false);
      }
    }
    init();
    return () => {
      active = false;
    };
  }, []);

  const handleFileSelect = async (path: string) => {
    setSelectedPath(path);
    setFileLoading(true);
    setFileError(null);
    setFileContent(null);
    try {
      const res = await nativeFs.readFile(path);
      if (res.ok && res.content !== undefined) {
        setFileContent(res.content);
      } else {
        setFileError(res.error || "Failed to read file");
      }
    } catch (err: any) {
      setFileError(err.message || "Error reading file");
    } finally {
      setFileLoading(false);
    }
  };

  const getFileName = (pathStr: string) => {
    const parts = pathStr.split(/[/\\]/);
    return parts[parts.length - 1] || pathStr;
  };

  if (!isDesktop) {
    return (
      <div className="flex items-center justify-center h-full p-4 text-center bg-panel">
        <div className="text-[11px] text-muted uppercase tracking-wider">
          File explorer requires the desktop app
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full overflow-hidden bg-panel">
      <div className="text-[10px] text-muted px-3 pt-2 uppercase tracking-wider flex items-center justify-between shrink-0">
        <span>Files ({getFileName(repoPath)})</span>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-2 flex flex-col gap-0.5">
        {loading && <div className="text-[11px] text-muted p-2">Loading workspace...</div>}
        {error && <div className="text-[11px] text-risk p-2">{error}</div>}
        {!loading && !error && rootNodes.length === 0 && (
          <div className="text-[11px] text-muted italic p-2">No files found</div>
        )}
        {!loading &&
          !error &&
          rootNodes.map((node) => (
            <TreeNode
              key={node.path}
              name={node.name}
              path={node.path}
              isDir={node.dir}
              onFileSelect={handleFileSelect}
              selectedPath={selectedPath}
            />
          ))}
      </div>

      <div className="h-1/2 border-t border-edge flex flex-col overflow-hidden bg-panel2 shrink-0">
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-edge bg-panel select-none shrink-0">
          <span className="text-[10px] text-muted uppercase tracking-wider truncate max-w-[80%]">
            {selectedPath ? getFileName(selectedPath) : "No file selected"}
          </span>
          {selectedPath && (
            <button
              onClick={() => {
                setSelectedPath(null);
                setFileContent(null);
                setFileError(null);
              }}
              className="text-muted hover:text-txt transition"
              title="Clear viewer"
            >
              <X size={12} />
            </button>
          )}
        </div>
        <div className="flex-1 overflow-auto bg-bg">
          {fileLoading && (
            <div className="text-[11px] text-muted p-3">Loading file contents...</div>
          )}
          {fileError && <div className="text-[11px] text-risk p-3">{fileError}</div>}
          {!fileLoading && !fileError && fileContent !== null && (
            <pre className="p-3 font-mono text-[12px] text-txt whitespace-pre leading-relaxed select-text">
              {fileContent}
            </pre>
          )}
          {!fileLoading && !fileError && fileContent === null && (
            <div className="text-[11px] text-muted italic p-3">
              Select a file above to view its contents
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
