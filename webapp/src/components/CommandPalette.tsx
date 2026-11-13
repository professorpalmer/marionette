import { Fragment, useEffect, useId, useMemo, useRef, useState } from "react";
import { Check, Cpu, FileText, MessageSquare, SquareTerminal, type LucideIcon } from "lucide-react";
import { focusSettingsPage } from "./SettingsShell";
import {
  buildPaletteSections,
  runCommandPaletteAction,
  shortcutCaps,
  shortcutText,
  type PaletteItem,
} from "../lib/commandPalette";
import { api, type Config, type Session, type SessionSearchHit } from "../lib/api";
import { OverlayPortal } from "../lib/overlayPortal";
import { requestSessionSwitch } from "../lib/sessionNav";
import { isMacNavigator } from "../lib/terminalSelection";

type CommandPaletteProps = {
  onToggleLeft: () => void;
  onToggleRight: () => void;
  config?: Config | null;
  sessionId?: string | null;
  /** False while a pilot swap is in flight. */
  modelsEnabled?: boolean;
  onPickModel?: (model: string) => void;
};

const SESSION_BANK_LIMIT = 200;
const FULL_TEXT_DELAY_MS = 150;

const KIND_ICON: Record<PaletteItem["kind"], LucideIcon> = {
  command: SquareTerminal,
  session: MessageSquare,
  file: FileText,
  model: Cpu,
};

function itemLabel(item: PaletteItem): string {
  return item.kind === "command" ? item.action.label : item.label;
}

function ShortcutCaps({ item, isMac }: { item: PaletteItem; isMac: boolean }) {
  if (item.kind === "model" && item.current) {
    return (
      <span className="inline-flex items-center gap-1 text-ui-11 text-faint">
        <Check size={12} strokeWidth={2} aria-hidden="true" />
        Current
      </span>
    );
  }
  if (item.kind !== "command" || !item.action.shortcut) return null;
  return (
    <span className="flex shrink-0 items-center gap-0.5" aria-label={shortcutText(item.action.shortcut, isMac)}>
      {shortcutCaps(item.action.shortcut, isMac).map((cap, index) => (
        <kbd
          key={index}
          aria-hidden="true"
          className="rounded border border-edge/60 px-1.5 py-0.5 text-center font-sans text-ui-11 leading-none text-faint"
        >
          {cap}
        </kbd>
      ))}
    </span>
  );
}

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
  config = null,
  sessionId = null,
  modelsEnabled = true,
  onPickModel,
}: CommandPaletteProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [files, setFiles] = useState<string[]>([]);
  const [sessionHits, setSessionHits] = useState<SessionSearchHit[]>([]);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const listId = useId();
  const isMac = useMemo(() => isMacNavigator(), []);

  const close = () => setOpen(false);

  // Each open reads the session bank and the workspace listing again, so a
  // new session or file shows up. A failed read leaves that section empty.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api.sessionsBank({ limit: SESSION_BANK_LIMIT })
      .then((rows) => { if (!cancelled) setSessions(Array.isArray(rows) ? rows : []); })
      .catch(() => { if (!cancelled) setSessions([]); });
    api.getWorkspaceFiles()
      .then((res) => { if (!cancelled) setFiles(Array.isArray(res?.files) ? res.files : []); })
      .catch(() => { if (!cancelled) setFiles([]); });
    return () => { cancelled = true; };
  }, [open]);

  // Full-text session recall finds a session by what was said in it.
  useEffect(() => {
    if (!open || !query.trim()) {
      setSessionHits([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api.searchSessions(query, 8)
        .then((hits) => { if (!cancelled) setSessionHits(hits); })
        .catch(() => { if (!cancelled) setSessionHits([]); });
    }, FULL_TEXT_DELAY_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [open, query]);

  const sections = buildPaletteSections(query, {
    sessions,
    sessionHits,
    currentSessionId: sessionId,
    files,
    models: config?.models?.length ? config.models : [config?.driver || ""].filter(Boolean),
    modelLabels: config?.model_labels,
    currentModel: config?.driver || "",
    modelsEnabled: modelsEnabled && Boolean(onPickModel),
  });
  const items = sections.flatMap((section) => section.items);
  const actionsRef = useRef(items);
  actionsRef.current = items;
  const activeIndexRef = useRef(activeIndex);
  activeIndexRef.current = activeIndex;
  const hooksRef = useRef({ onToggleLeft, onToggleRight, onPickModel });
  hooksRef.current = { onToggleLeft, onToggleRight, onPickModel };

  const runSelected = (item: PaletteItem) => {
    switch (item.kind) {
      case "command":
        runCommandPaletteAction(item.action.id, {
          toggleLeft: hooksRef.current.onToggleLeft,
          toggleRight: hooksRef.current.onToggleRight,
          focusSettingsPage: (page) => focusSettingsPage(page as "advanced"),
        });
        break;
      case "session":
        requestSessionSwitch(item.sessionId);
        break;
      case "file":
        window.dispatchEvent(new CustomEvent("harness-open-file", { detail: { path: item.path, trusted: true } }));
        break;
      case "model":
        if (!item.current) hooksRef.current.onPickModel?.(item.model);
        break;
      default: {
        const _exhaustive: never = item;
        void _exhaustive;
      }
    }
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
    setActiveIndex((i) => (items.length === 0 ? 0 : Math.min(i, items.length - 1)));
  }, [items.length, query]);

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
        const item = list[activeIndexRef.current];
        if (!item) return;
        e.preventDefault();
        e.stopPropagation();
        runSelectedRef.current(item);
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
        className="w-full max-w-xl rounded-lg border border-edge bg-panel shadow-2xl overflow-hidden"
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
            placeholder="Search sessions, files, commands and models"
            aria-label="Search sessions, files, commands and models"
            className="w-full bg-transparent text-ui-13 text-txt placeholder:text-faint outline-none"
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
          aria-label="Results"
          data-testid="command-palette-list"
          className="max-h-96 overflow-y-auto py-1"
        >
          {items.length === 0 ? (
            <div className="px-3 py-2.5 text-ui-12 text-muted">No matches</div>
          ) : (
            sections.map((section) => {
              const offset = items.indexOf(section.items[0]);
              return (
                <Fragment key={section.id}>
                  <div
                    role="presentation"
                    data-testid={`command-palette-section-${section.id}`}
                    className="px-3 pb-1 pt-2 text-ui-11 font-medium text-faint"
                  >
                    {section.title}
                  </div>
                  {section.items.map((item, at) => {
                    const index = offset + at;
                    const active = index === activeIndex;
                    const Icon = KIND_ICON[item.kind];
                    const detail = item.kind === "command" ? "" : item.detail;
                    return (
                      <button
                        key={item.key}
                        type="button"
                        role="option"
                        aria-selected={active}
                        data-palette-index={index}
                        data-testid={item.kind === "command"
                          ? `command-palette-item-${item.action.id}`
                          : `command-palette-${item.kind}`}
                        className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-ui-12.5 transition-colors ${
                          active ? "bg-panel2 text-txt" : "text-muted hover:bg-panel2/50 hover:text-txt"
                        }`}
                        onMouseEnter={() => setActiveIndex(index)}
                        onClick={() => runSelected(item)}
                      >
                        <Icon size={14} strokeWidth={1.5} aria-hidden="true" className="shrink-0 text-faint" />
                        <span className="min-w-0 flex-1 truncate">
                          <span className={active ? "text-txt" : "text-muted"}>{itemLabel(item)}</span>
                          {detail && <span className="ml-2 text-ui-11 text-faint">{detail}</span>}
                        </span>
                        <ShortcutCaps item={item} isMac={isMac} />
                      </button>
                    );
                  })}
                </Fragment>
              );
            })
          )}
        </div>
        <div className="flex items-center gap-3 border-t border-edge/50 px-3 py-1.5 text-ui-11 text-faint">
          <span>{"\u2191\u2193"} to move</span>
          <span>Enter to open</span>
          <span>Esc to close</span>
          <span className="ml-auto">{shortcutCaps(["mod"], isMac)[0]}K toggles</span>
        </div>
      </div>
    </OverlayPortal>
  );
}
