import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import { api, type Config, type ReasoningEffort } from "../lib/api";
import { REASONING_LEVELS, labelForEffort } from "../lib/reasoningSupport";
import { useOverlayFocus } from "../lib/overlayFocus";

export default function SwarmReasoningPicker({ config }: { config: Config | null }) {
  const [effort, setEffort] = useState<ReasoningEffort>("medium");
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useOverlayFocus(open, menuRef, {
    onClose: () => setOpen(false),
  });

  useEffect(() => {
    if (!config) return;
    setEffort(config.swarm_reasoning_effort || "medium");
  }, [config]);

  useEffect(() => {
    if (!open) return;
    const handleOutsideClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleOutsideClick);
    return () => document.removeEventListener("mousedown", handleOutsideClick);
  }, [open]);

  const setWorkerEffort = async (level: ReasoningEffort) => {
    const prev = effort;
    setEffort(level);
    setOpen(false);
    try {
      await api.updateSettings({ swarm_reasoning_effort: level });
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch {
      setEffort(prev);
      window.dispatchEvent(new CustomEvent("harness-toast", {
        detail: "Worker reasoning setting failed -- try again",
      }));
    }
  };

  return (
    <div ref={rootRef} className="relative shrink-0" data-testid="swarm-reasoning-picker">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        title="Worker reasoning for swarms and implement (not the chat pilot)"
        className="flex items-center gap-1 text-[11px] text-muted hover:text-txt rounded-md px-2 h-[22px] bg-transparent hover:bg-panel2 border border-edge/40 transition select-none"
      >
        <span className="composer-toolbar-label">Workers</span>
        <span className="truncate max-w-[72px]">{labelForEffort(effort)}</span>
        <ChevronDown size={11} className="shrink-0 opacity-60" />
      </button>
      {open && (
        <div
          ref={menuRef}
          role="dialog"
          aria-modal="true"
          aria-label="Worker reasoning picker"
          className="absolute left-0 bottom-full mb-1 z-50 min-w-[140px] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden"
        >
          {REASONING_LEVELS.map(({ value, label }) => {
            const isSelected = value === effort;
            return (
              <div
                key={value}
                onClick={() => setWorkerEffort(value)}
                className={`flex items-center justify-between px-3 py-1.5 text-[11.5px] hover:bg-panel2 cursor-pointer transition select-none ${
                  isSelected ? "text-accent font-medium bg-panel2/40" : "text-txt/90"
                }`}
              >
                <span>{label}</span>
                {isSelected && <Check size={11} className="shrink-0 ml-2" />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
