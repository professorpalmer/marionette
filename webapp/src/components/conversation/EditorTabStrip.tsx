/**
 * File-editor tab strip + right-click context menu for Conversation.
 */

import { X } from "lucide-react";
import { revealInFolderLabel, revealWorkspacePath, toAbsoluteWorkspacePath } from "../../lib/transport";

export type OpenEditorTab = {
  path: string;
  isDirty: boolean;
  line?: number;
  col?: number;
};

export type TabContextMenuState = {
  x: number;
  y: number;
  path: string;
};

export default function EditorTabStrip({
  openTabs,
  activeTab,
  tabContextMenu,
  repoRoot,
  onSelectTab,
  onCloseTab,
  onCloseOtherTabs,
  onCloseAllTabs,
  onOpenContextMenu,
  onCloseContextMenu,
}: {
  openTabs: OpenEditorTab[];
  activeTab: string;
  tabContextMenu: TabContextMenuState | null;
  repoRoot: string;
  onSelectTab: (path: string) => void;
  onCloseTab: (path: string) => void;
  onCloseOtherTabs: (keepPath: string) => void;
  onCloseAllTabs: () => void;
  onOpenContextMenu: (menu: TabContextMenuState) => void;
  onCloseContextMenu: () => void;
}) {
  if (openTabs.length === 0 && !tabContextMenu) return null;

  return (
    <>
      {openTabs.length > 0 && (
        <div className="flex items-center gap-1 px-4 bg-panel border-b border-edge h-9 shrink-0 overflow-x-auto scrollbar-none select-none">
          <button
            onClick={() => onSelectTab("chat")}
            className={`flex items-center h-full px-3 text-[12px] font-medium transition-colors border-b-2 ${
              activeTab === "chat"
                ? "border-accent text-accent bg-bg/50"
                : "border-transparent text-muted hover:text-txt"
            }`}
          >
            Chat
          </button>
          {openTabs.map((t) => {
            const filename = t.path.split(/[/\\]/).pop() || t.path;
            const isSelected = activeTab === t.path;
            return (
              <div
                key={t.path}
                className={`flex items-center h-full px-2 text-[12px] font-medium transition-colors border-b-2 group relative ${
                  isSelected
                    ? "border-accent text-accent bg-bg/50"
                    : "border-transparent text-muted hover:text-txt"
                }`}
                onContextMenu={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  onOpenContextMenu({ x: e.clientX, y: e.clientY, path: t.path });
                }}
              >
                <button
                  onClick={() => onSelectTab(t.path)}
                  className="flex items-center gap-1.5 h-full max-w-[150px]"
                  title={t.path}
                >
                  {t.isDirty && (
                    <span className="w-1.5 h-1.5 rounded-full bg-warn shrink-0" />
                  )}
                  <span className="truncate">{filename}</span>
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onCloseTab(t.path);
                  }}
                  className="ml-2 p-0.5 rounded hover:bg-panel2 text-muted hover:text-txt opacity-60 group-hover:opacity-100 transition-opacity"
                >
                  <X size={10} />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {tabContextMenu && (
        <div
          className="fixed z-50 bg-panel border border-edge rounded shadow-lg text-[12px] py-1 min-w-[160px]"
          style={{ top: tabContextMenu.y, left: tabContextMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={async () => {
              const path = tabContextMenu.path;
              onCloseContextMenu();
              const res = await revealWorkspacePath(repoRoot, path);
              if (!res.ok) {
                window.dispatchEvent(
                  new CustomEvent("harness-toast", {
                    detail: res.error || "Could not reveal path",
                  }),
                );
              }
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            {revealInFolderLabel()}
          </button>
          <button
            onClick={async () => {
              const path = tabContextMenu.path;
              onCloseContextMenu();
              const abs = toAbsoluteWorkspacePath(repoRoot, path);
              try {
                await navigator.clipboard.writeText(abs);
                window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Path copied" }));
              } catch {
                window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Could not copy path" }));
              }
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Copy Path
          </button>
          <button
            onClick={async () => {
              const path = tabContextMenu.path;
              onCloseContextMenu();
              try {
                await navigator.clipboard.writeText(path.replace(/\\/g, "/"));
                window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Relative path copied" }));
              } catch {
                window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Could not copy path" }));
              }
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Copy Relative Path
          </button>
          <div className="border-t border-edge my-1" />
          <button
            onClick={() => {
              const path = tabContextMenu.path;
              onCloseContextMenu();
              onCloseTab(path);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Close
          </button>
          <button
            onClick={() => {
              const path = tabContextMenu.path;
              onCloseContextMenu();
              onCloseOtherTabs(path);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Close others
          </button>
          <button
            onClick={() => {
              onCloseContextMenu();
              onCloseAllTabs();
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Close all
          </button>
        </div>
      )}
    </>
  );
}
