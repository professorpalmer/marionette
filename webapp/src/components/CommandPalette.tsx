import { useEffect, useId, useRef, useState } from "react";
import { focusSettingsPage } from "./SettingsShell";
import {
  COMMAND_PALETTE_ACTIONS,
  filterCommandPaletteActions,
  runCommandPaletteAction,
  type CommandPaletteAction,
} from "../lib/commandPalette";
import { OverlayPortal } from "../lib/overlayPortal";

type CommandPaletteProps = {
  onToggleLeft: () => void;
  onToggleRight: () => void;
};

function isPaletteShortcut(e: KeyboardEvent): boolean {
  if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return false;
  return e.key.toLowerCase() === "k";
}

/**
 * Global Cmd/Ctrl-K operator palette. Mounted once near App so it works
 * even when the composer is unfocused.
 */
export default function CommandPalette({
  onToggleLeft,
  onToggleRight,
}: CommandPaletteProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const listId = useId();

  const close = () => setOpen(false);

  const actions = filterCommandPaletteActions(COMMAND_PALETTE_ACTIONS, query);
  const actionsRef = useRef(actions);
  actionsRef.current = actions;
  const activeIndexRef = useRef(activeIndex);
  activeIndexRef.current = activeIndex;
  const hooksRef = useRef({ onToggleLeft, onToggleRight });
  hooksRef.current = { onToggleLeft, onToggleRight };

  const runSelected = (action: CommandPaletteAction) => {
    runCommandPaletteAction(action.id, {
      toggleLeft: hooksRef.current.onToggleLeft,
      toggleRight: hooksRef.current.onToggleRight,
      focusSettingsPage: (page) => focusSettingsPage(page as "advanced"),
    });
    setOpen(false);
    setQuery("");
  };
  const runSelectedRef = useRef(runSelected);
  runSelectedRef.current = runSelected;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!isPaletteShortcut(e)) return;
      e.preventDefault();
      e.stopPropagation();
      setOpen((wasOpen) => {
        if (wasOpen) return false;
        setQuery("");
        setActiveIndex(0);
        return true;
      });
    };
    window.addEventListener("keydown", onKey, true);
    const onOpen = () => {
      setQuery("");
      setActiveIndex(0);
      setOpen(true);
    };
    window.addEventListener("harness-open-command-palette", onOpen);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("harness-open-command-palette", onOpen);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    setActiveIndex(0);
    const t = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [open]);

  useEffect(() => {
    setActiveIndex((i) => (actions.length === 0 ? 0 : Math.min(i, actions.length - 1)));
  }, [actions.length, query]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      const list = actionsRef.current;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((i) => (list.length === 0 ? 0 : (i + 1) % list.length));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((i) =>
          list.length === 0 ? 0 : (i - 1 + list.length) % list.length,
        );
        return;
      }
      if (e.key === "Enter") {
        const action = list[activeIndexRef.current];
        if (!action) return;
        e.preventDefault();
        e.stopPropagation();
        runSelectedRef.current(action);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open]);

  useEffect(() => {
    if (!open || !listRef.current) return;
    const row = listRef.current.querySelector<HTMLElement>(
      `[data-palette-index="${activeIndex}"]`,
    );
    row?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, open]);

  return (
    <OverlayPortal
      open={open}
      onClose={close}
      focusRootRef={dialogRef}
      initialFocusRef={inputRef}
      testId="command-palette-backdrop"
      className="fixed inset-0 z-[60] bg-black/55 flex items-start justify-center pt-[18vh] px-4 transition-opacity duration-150"
      onBackdropMouseDown={(e) => {
        if (e.target === e.currentTarget) setOpen(false);
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        data-testid="command-palette"
        className="w-full max-w-lg rounded-lg border border-edge bg-panel shadow-2xl overflow-hidden"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="border-b border-edge/50 px-3 py-2">
          <input
            ref={inputRef}
            data-testid="command-palette-input"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            placeholder="Run a command…"
            className="w-full bg-transparent text-[13px] text-txt placeholder:text-faint outline-none"
            aria-controls={listId}
            aria-autocomplete="list"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        <div
          ref={listRef}
          id={listId}
          role="listbox"
          data-testid="command-palette-list"
          className="max-h-72 overflow-y-auto py-1"
        >
          {actions.length === 0 ? (
            <div className="px-3 py-2.5 text-[12px] text-muted">No matching commands</div>
          ) : (
            actions.map((action, index) => {
              const active = index === activeIndex;
              return (
                <button
                  key={action.id}
                  type="button"
                  role="option"
                  aria-selected={active}
                  data-palette-index={index}
                  data-testid={`command-palette-item-${action.id}`}
                  className={`w-full text-left px-3 py-2 text-[12.5px] transition-colors ${
                    active
                      ? "bg-panel2 text-txt"
                      : "text-muted hover:bg-panel2/50 hover:text-txt"
                  }`}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => runSelected(action)}
                >
                  {action.label}
                </button>
              );
            })
          )}
        </div>
      </div>
    </OverlayPortal>
  );
}
