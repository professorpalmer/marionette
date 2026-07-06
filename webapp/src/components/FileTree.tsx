import { useState, useEffect } from "react";
import { ChevronRight, ChevronDown, File, RefreshCw } from "lucide-react";
import { api } from "../lib/api";

interface FileNode {
  name: string;
  path: string;
  isDir: boolean;
  children?: FileNode[];
}

interface TreeNodeProps {
  node: FileNode;
  onFileSelect: (path: string) => void;
  selectedPath: string | null;
}

function TreeNode({ node, onFileSelect, selectedPath }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(false);

  const toggleExpand = () => {
    if (!node.isDir) {
      onFileSelect(node.path);
      return;
    }
    setExpanded(!expanded);
  };

  return (
    <div className="select-none font-sans">
      <div
        onClick={toggleExpand}
        className={`flex items-center gap-1.5 py-1 px-1.5 rounded cursor-pointer text-[12px] hover:bg-panel2/80 transition ${
          selectedPath === node.path ? "bg-panel2 text-accent" : "text-txt"
        }`}
      >
        {node.isDir ? (
          <>
            {expanded ? (
              <ChevronDown size={14} className="text-muted shrink-0" />
            ) : (
              <ChevronRight size={14} className="text-muted shrink-0" />
            )}
            <span className="truncate font-medium">{node.name}</span>
          </>
        ) : (
          <>
            <File size={14} className="text-muted shrink-0 ml-[14px]" />
            <span className="truncate">{node.name}</span>
          </>
        )}
      </div>

      {node.isDir && expanded && node.children && (
        <div className="pl-3 border-l border-edge/40 ml-2.5 mt-0.5 mb-1 flex flex-col gap-0.5">
          {node.children.map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              onFileSelect={onFileSelect}
              selectedPath={selectedPath}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function buildTree(paths: string[]): FileNode[] {
  const root: FileNode[] = [];
  const map: Record<string, FileNode> = {};

  for (const path of paths) {
    // Windows backends emit backslash-separated paths; without this the whole
    // path collapses into a single flat node.
    const parts = path.split(/[\\/]/);
    let currentPath = "";
    let parentChildren = root;

    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      currentPath = currentPath ? `${currentPath}/${part}` : part;
      const isLast = i === parts.length - 1;

      if (!map[currentPath]) {
        const node: FileNode = {
          name: part,
          path: currentPath,
          isDir: !isLast,
          children: isLast ? undefined : []
        };
        map[currentPath] = node;
        parentChildren.push(node);
      }

      const node = map[currentPath];
      if (node.isDir && node.children) {
        parentChildren = node.children;
      }
    }
  }

  const sortTree = (nodes: FileNode[]) => {
    nodes.sort((a, b) => {
      if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const node of nodes) {
      if (node.children) {
        sortTree(node.children);
      }
    }
  };
  sortTree(root);

  return root;
}

export default function FileTree() {
  const [repoName, setRepoName] = useState<string>("");
  const [rootNodes, setRootNodes] = useState<FileNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  const loadFiles = async () => {
    setLoading(true);
    setError(null);
    try {
      const cfg = await api.config();
      const workspacePath = cfg.repo || "";
      const repoNameFromPath = workspacePath.split(/[/\\]/).pop() || "workspace";
      setRepoName(repoNameFromPath);

      const res = await api.getWorkspaceFiles();
      if (res && res.files) {
        const tree = buildTree(res.files);
        setRootNodes(tree);
      } else {
        setError("Failed to get workspace files");
      }
    } catch (err: any) {
      setError(err.message || "Error loading workspace files");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadFiles();

    // Listen to changes that might require refreshing files
    const handleRefresh = () => {
      loadFiles();
    };

    window.addEventListener("harness-config-changed", handleRefresh);
    window.addEventListener("harness-file-saved", handleRefresh);
    window.addEventListener("harness-file-edited", handleRefresh);

    return () => {
      window.removeEventListener("harness-config-changed", handleRefresh);
      window.removeEventListener("harness-file-saved", handleRefresh);
      window.removeEventListener("harness-file-edited", handleRefresh);
    };
  }, []);

  const handleFileSelect = (path: string) => {
    setSelectedPath(path);
    // Dispatch custom event to let CenterPane/Conversation know we want to open this file
    window.dispatchEvent(new CustomEvent("harness-open-file", { detail: { path } }));
  };

  return (
    <div className="flex flex-col h-full overflow-hidden bg-panel">
      <div className="text-[10px] text-muted px-3 py-2 uppercase tracking-wider flex items-center justify-between shrink-0 border-b border-edge/30">
        <span>Files ({repoName || "unknown"})</span>
        <button
          onClick={loadFiles}
          className="p-1 hover:bg-panel2 rounded transition text-muted hover:text-txt"
          title="Refresh file tree"
          disabled={loading}
        >
          <RefreshCw size={11} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-2 flex flex-col gap-0.5">
        {loading && rootNodes.length === 0 && (
          <div className="text-[11px] text-muted p-2">Loading workspace...</div>
        )}
        {error && <div className="text-[11px] text-risk p-2">{error}</div>}
        {!loading && !error && rootNodes.length === 0 && (
          <div className="text-[11px] text-muted italic p-2">No files found</div>
        )}
        {rootNodes.map((node) => (
          <TreeNode
            key={node.path}
            node={node}
            onFileSelect={handleFileSelect}
            selectedPath={selectedPath}
          />
        ))}
      </div>
    </div>
  );
}
