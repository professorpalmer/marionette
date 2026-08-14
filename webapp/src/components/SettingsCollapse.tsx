import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";

const SETTINGS_SECTION_OPEN_KEY = "pmharness.settings.sectionOpen";

function loadSettingsSectionOpen(id: string, defaultOpen: boolean): boolean {
  try {
    const raw = localStorage.getItem(SETTINGS_SECTION_OPEN_KEY);
    if (!raw) return defaultOpen;
    const map = JSON.parse(raw) as Record<string, boolean>;
    if (typeof map[id] === "boolean") return map[id];
  } catch {
    /* ignore */
  }
  return defaultOpen;
}

function persistSettingsSectionOpen(id: string, open: boolean) {
  try {
    const raw = localStorage.getItem(SETTINGS_SECTION_OPEN_KEY);
    const map = (raw ? JSON.parse(raw) : {}) as Record<string, boolean>;
    map[id] = open;
    localStorage.setItem(SETTINGS_SECTION_OPEN_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

/** Collapsible settings block — chevron title + optional summary; persists open state. */
export function SettingsCollapse({
  id,
  title,
  summary,
  defaultOpen = true,
  forceOpen = false,
  className = "space-y-2 border-t border-edge pt-3",
  onFirstOpen,
  children,
}: {
  id: string;
  title: string;
  summary?: string;
  defaultOpen?: boolean;
  forceOpen?: boolean;
  className?: string;
  onFirstOpen?: () => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(() => loadSettingsSectionOpen(id, defaultOpen));
  const shown = forceOpen || open;
  const firstOpenCalled = useRef(false);

  useEffect(() => {
    if (shown && onFirstOpen && !firstOpenCalled.current) {
      firstOpenCalled.current = true;
      onFirstOpen();
    }
  }, [shown, onFirstOpen]);

  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => {
          setOpen((v) => {
            const next = !v;
            persistSettingsSectionOpen(id, next);
            return next;
          });
        }}
        className="w-full flex items-center justify-between gap-2 text-left focus:outline-none group"
      >
        <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1.5 min-w-0 group-hover:text-txt transition">
          {shown ? <ChevronDown size={12} className="shrink-0" /> : <ChevronRight size={12} className="shrink-0" />}
          <span className="truncate">{title}</span>
          {summary ? (
            <span className="normal-case tracking-normal font-normal text-faint/80 shrink-0">
              · {summary}
            </span>
          ) : null}
        </span>
      </button>
      {shown ? <div className="space-y-2">{children}</div> : null}
    </div>
  );
}
