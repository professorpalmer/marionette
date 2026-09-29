import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { api, type Config, type ReasoningEffort } from "../lib/api";
import { labelForEffort } from "../lib/reasoningSupport";
import ReasoningLevelOptions from "./ReasoningLevelOptions";
import { useOverlayFocus } from "../lib/overlayFocus";

export default function SwarmReasoningPicker({ config, sessionId = "" }: { config: Config | null; sessionId?: string }) {
  const [effort, setEffort] = useState<ReasoningEffort>("medium");
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const selectedRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const operation = useRef(0);
  useEffect(() => () => { operation.current++; }, [sessionId]);

  useOverlayFocus(open, menuRef, {
    initialFocusRef: selectedRef,
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
    if (!sessionId || !config) return;
    const generation = ++operation.current;
    const prev = effort;
    setEffort(level);
    setOpen(false);
    try {
      await api.setPilotPreferences(sessionId, { swarm_reasoning_effort: level });
      if (generation !== operation.current) return;
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch {
      if (generation !== operation.current) return;
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
        aria-haspopup="dialog"
        aria-expanded={open}
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
          <ReasoningLevelOptions value={effort} onSelect={setWorkerEffort} selectedRef={selectedRef} />
        </div>
      )}
    </div>
  );
}
