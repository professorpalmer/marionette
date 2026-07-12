import { useState, useEffect, useRef } from "react";
import { ChevronRight, ChevronDown, File, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import {
  revealInFolderLabel,
  revealWorkspacePath,
  toAbsoluteWorkspacePath,
} from "../lib/transport";

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
  onContextMenu: (e: React.MouseEvent, node: FileNode) => void;
}

type FileContextMenu = {
  x: number;
  y: number;
  /** null = empty space / workspace root */
  node: FileNode | null;
};

/** In-app name prompt -- Electron disables window.prompt, which made New/Rename no-ops. */
type NamePromptState = {
  mode: "new-file" | "new-folder" | "rename";
  title: string;
  initial: string;
  dir: string;
  node: FileNode | null;
};

function TreeNode({ node, onFileSelect, selectedPath, onContextMenu }: TreeNodeProps) {
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
        onContextMenu={(e) => onContextMenu(e, node)}
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
              onContextMenu={onContextMenu}
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

function parentDir(filePath: string): string {
  const parts = filePath.replace(/\\/g, "/").split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function joinRel(...parts: string[]): string {
  return parts.filter(Boolean).join("/");
}

function toast(msg: string) {
  window.dispatchEvent(new CustomEvent("harness-toast", { detail: msg }));
}

function notifyTreeMutated(paths?: { deleted?: string; renamed?: { from: string; to: string } }) {
  window.dispatchEvent(new Event("harness-file-edited"));
  window.dispatchEvent(new Event("harness-file-saved"));
  if (paths?.deleted) {
    window.dispatchEvent(
      new CustomEvent("harness-file-deleted", { detail: { path: paths.deleted } }),
    );
  }
  if (paths?.renamed) {
    window.dispatchEvent(
      new CustomEvent("harness-file-renamed", { detail: paths.renamed }),
    );
  }
}

export default function FileTree() {
  const [repoName, setRepoName] = useState<string>("");
  const [repoRoot, setRepoRoot] = useState<string>("");
  const [rootNodes, setRootNodes] = useState<FileNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<FileContextMenu | null>(null);
  const [confirmDeletePath, setConfirmDeletePath] = useState<string | null>(null);
  const [namePrompt, setNamePrompt] = useState<NamePromptState | null>(null);
  const [nameValue, setNameValue] = useState("");
  const nameInputRef = useRef<HTMLInputElement | null>(null);

  const loadFiles = async () => {
    setLoading(true);
    setError(null);
    try {
      const cfg = await api.config();
      const workspacePath = cfg.repo || "";
      setRepoRoot(workspacePath);
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
    // Electron: main fires this after backend respawn / port refresh so a
    // transient ECONNREFUSED does not stick in the Files panel.
    const ipc: any = (typeof window !== "undefined" && (window as any).harnessIPC) || null;
    const unsubRespawn = typeof ipc?.onBackendRespawned === "function"
      ? ipc.onBackendRespawned(() => { void loadFiles(); })
      : null;

    return () => {
      window.removeEventListener("harness-config-changed", handleRefresh);
      window.removeEventListener("harness-file-saved", handleRefresh);
      window.removeEventListener("harness-file-edited", handleRefresh);
      try { unsubRespawn?.(); } catch { /* ignore */ }
    };
  }, []);

  useEffect(() => {
    if (!contextMenu) return;
    const handleClose = () => {
      setContextMenu(null);
      setConfirmDeletePath(null);
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleClose();
    };
    window.addEventListener("click", handleClose);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("click", handleClose);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [contextMenu]);

  useEffect(() => {
    if (!namePrompt) return;
    setNameValue(namePrompt.initial);
    const t = window.setTimeout(() => {
      nameInputRef.current?.focus();
      nameInputRef.current?.select();
    }, 0);
    return () => window.clearTimeout(t);
  }, [namePrompt]);

  const handleFileSelect = (path: string) => {
    setSelectedPath(path);
    // Dispatch custom event to let CenterPane/Conversation know we want to open this file
    window.dispatchEvent(new CustomEvent("harness-open-file", { detail: { path } }));
  };

  const openContextMenu = (e: React.MouseEvent, node: FileNode | null) => {
    e.preventDefault();
    e.stopPropagation();
    setConfirmDeletePath(null);
    setContextMenu({ x: e.clientX, y: e.clientY, node });
  };

  const closeMenu = () => {
    setContextMenu(null);
    setConfirmDeletePath(null);
  };

  const targetDirForCreate = (node: FileNode | null): string => {
    if (!node) return "";
    if (node.isDir) return node.path;
    return parentDir(node.path);
  };

  const validateBaseName = (name: string): string | null => {
    const trimmed = name.trim();
    if (!trimmed) return "Name is required";
    if (trimmed.includes("/") || trimmed.includes("\\") || trimmed === "." || trimmed === "..") {
      return "Invalid name";
    }
    return null;
  };

  const handleReveal = async () => {
    const node = contextMenu?.node;
    if (!node) return;
    closeMenu();
    const res = await revealWorkspacePath(repoRoot, node.path);
    if (!res.ok) toast(res.error || "Could not reveal path");
  };

  // Hermes file-menu polish: copy absolute + workspace-relative paths.
  const handleCopyPath = async (relative: boolean) => {
    const node = contextMenu?.node;
    if (!node) return;
    closeMenu();
    const text = relative
      ? node.path.replace(/\\/g, "/")
      : toAbsoluteWorkspacePath(repoRoot, node.path);
    try {
      await navigator.clipboard.writeText(text);
      toast(relative ? "Relative path copied" : "Path copied");
    } catch {
      toast("Could not copy path");
    }
  };

  const handleOpen = () => {
    const node = contextMenu?.node;
    if (!node || node.isDir) return;
    closeMenu();
    handleFileSelect(node.path);
  };

  const openNewFilePrompt = () => {
    const node = contextMenu?.node ?? null;
    const dir = targetDirForCreate(node);
    closeMenu();
    setNamePrompt({ mode: "new-file", title: "New file", initial: "", dir, node });
  };

  const openNewFolderPrompt = () => {
    const node = contextMenu?.node ?? null;
    const dir = targetDirForCreate(node);
    closeMenu();
    setNamePrompt({ mode: "new-folder", title: "New folder", initial: "", dir, node });
  };

  const openRenamePrompt = () => {
    const node = contextMenu?.node;
    if (!node) return;
    closeMenu();
    setNamePrompt({
      mode: "rename",
      title: "Rename",
      initial: node.name,
      dir: parentDir(node.path),
      node,
    });
  };

  const submitNamePrompt = async () => {
    if (!namePrompt) return;
    const invalid = validateBaseName(nameValue);
    if (invalid) {
      toast(invalid);
      return;
    }
    const trimmed = nameValue.trim();
    const prompt = namePrompt;
    setNamePrompt(null);

    if (prompt.mode === "new-file") {
      const rel = joinRel(prompt.dir, trimmed);
      try {
        const res = await api.writeFile(rel, "");
        if (!res.ok) {
          toast(res.error || "Could not create file");
          return;
        }
        await loadFiles();
        notifyTreeMutated();
        handleFileSelect(rel);
      } catch (err: any) {
        toast(err?.error || err?.message || "Could not create file");
      }
      return;
    }

    if (prompt.mode === "new-folder") {
      const rel = joinRel(prompt.dir, trimmed);
      try {
        const res = await api.mkdir(rel);
        if (!res.ok) {
          toast(res.error || "Could not create folder");
          return;
        }
        await loadFiles();
        notifyTreeMutated();
      } catch (err: any) {
        toast(err?.error || err?.message || "Could not create folder");
      }
      return;
    }

    // rename
    const node = prompt.node;
    if (!node) return;
    if (trimmed === node.name) return;
    try {
      const res = await api.renameFile({ path: node.path, new_name: trimmed });
      if (!res.ok) {
        toast(res.error || "Could not rename");
        return;
      }
      const to = res.to || joinRel(parentDir(node.path), trimmed);
      await loadFiles();
      notifyTreeMutated({
        renamed: { from: node.path, to },
      });
      if (selectedPath === node.path) setSelectedPath(to);
      if (!node.isDir) handleFileSelect(to);
    } catch (err: any) {
      toast(err?.error || err?.message || "Could not rename");
    }
  };

  const handleDeleteConfirmed = async () => {
    const node = contextMenu?.node;
    if (!node) return;
    const deletedPath = node.path;
    closeMenu();
    try {
      const res = await api.deleteFile(deletedPath);
      if (!res.ok) {
        toast(res.error || "Could not delete");
        return;
      }
      await loadFiles();
      notifyTreeMutated({ deleted: deletedPath });
      if (selectedPath === deletedPath || selectedPath?.startsWith(deletedPath + "/")) {
        setSelectedPath(null);
      }
    } catch (err: any) {
      toast(err?.error || err?.message || "Could not delete");
    }
  };

  const node = contextMenu?.node ?? null;
  const isFile = !!node && !node.isDir;
  const canMutateNode = !!node;
  const revealLabel = revealInFolderLabel();

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

      <div
        className="flex-1 overflow-y-auto px-2 py-2 flex flex-col gap-0.5"
        onContextMenu={(e) => {
          // Empty-space / background: create at workspace root.
          if ((e.target as HTMLElement).closest("[data-file-tree-node]")) return;
          openContextMenu(e, null);
        }}
      >
        {loading && rootNodes.length === 0 && (
          <div className="text-[11px] text-muted p-2">Loading workspace...</div>
        )}
        {error && <div className="text-[11px] text-risk p-2">{error}</div>}
        {!loading && !error && rootNodes.length === 0 && (
          <div className="text-[11px] text-muted italic p-2">No files found</div>
        )}
        {rootNodes.map((n) => (
          <div key={n.path} data-file-tree-node>
            <TreeNode
              node={n}
              onFileSelect={handleFileSelect}
              selectedPath={selectedPath}
              onContextMenu={openContextMenu}
            />
          </div>
        ))}
      </div>

      {contextMenu && (
        <div
          className="fixed z-50 bg-panel border border-edge rounded shadow-lg text-[12px] py-1 min-w-[160px]"
          style={{ top: contextMenu.y, left: contextMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          {isFile && (
            <button
              onClick={handleOpen}
              className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
            >
              Open
            </button>
          )}
          {node && (
            <button
              onClick={() => void handleReveal()}
              className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
            >
              {revealLabel}
            </button>
          )}
          {node && (
            <>
              <button
                onClick={() => void handleCopyPath(false)}
                className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
              >
                Copy Path
              </button>
              <button
                onClick={() => void handleCopyPath(true)}
                className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
              >
                Copy Relative Path
              </button>
            </>
          )}
          {(isFile || node) && (
            <div className="border-t border-edge my-1" />
          )}
          <button
            onClick={openNewFilePrompt}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            New File…
          </button>
          <button
            onClick={openNewFolderPrompt}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            New Folder…
          </button>
          {canMutateNode && (
            <>
              <div className="border-t border-edge my-1" />
              <button
                onClick={openRenamePrompt}
                className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
              >
                Rename…
              </button>
              {confirmDeletePath === node!.path ? (
                <div className="px-3 py-1.5 flex items-center justify-between gap-2 bg-panel2/50">
                  <span className="text-muted font-medium">Delete?</span>
                  <div className="flex gap-2">
                    <button
                      onClick={() => void handleDeleteConfirmed()}
                      className="text-red-400 font-bold hover:underline"
                    >
                      Yes
                    </button>
                    <button
                      onClick={() => setConfirmDeletePath(null)}
                      className="text-muted hover:underline"
                    >
                      No
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  onClick={() => setConfirmDeletePath(node!.path)}
                  className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-red-400 font-medium transition-colors"
                >
                  Delete…
                </button>
              )}
            </>
          )}
        </div>
      )}

      {namePrompt && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/45"
          onClick={() => setNamePrompt(null)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setNamePrompt(null);
          }}
        >
          <div
            className="w-[min(360px,92vw)] rounded-lg border border-edge bg-panel shadow-xl p-3 flex flex-col gap-2"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="text-[12px] font-medium text-txt">{namePrompt.title}</div>
            <input
              ref={nameInputRef}
              value={nameValue}
              onChange={(e) => setNameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submitNamePrompt();
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setNamePrompt(null);
                }
              }}
              className="w-full rounded border border-edge bg-bg px-2 py-1.5 text-[12px] text-txt outline-none focus:border-accent"
              placeholder={namePrompt.mode === "new-folder" ? "folder-name" : "name"}
              spellCheck={false}
            />
            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={() => setNamePrompt(null)}
                className="px-2.5 py-1 rounded text-[11px] text-muted hover:bg-panel2"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void submitNamePrompt()}
                className="px-2.5 py-1 rounded text-[11px] bg-accent/20 text-accent hover:bg-accent/30 font-medium"
              >
                OK
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
