import { useEffect, useRef, useState } from "react";
import { Menu } from "@base-ui/react/menu";
import { ChevronDown, Users } from "lucide-react";
import { api, type Config, type ReasoningEffort } from "../lib/api";
import { labelForEffort } from "../lib/reasoningSupport";
import ReasoningLevelOptions from "./ReasoningLevelOptions";
import { MenuPanel, MenuRoot } from "./ui/overlay";

export default function SwarmReasoningPicker({ config, sessionId = "" }: { config: Config | null; sessionId?: string }) {
  // Remounts on session switch: seed from config so the first paint is right.
  const [effort, setEffort] = useState<ReasoningEffort>(() => config?.swarm_reasoning_effort || "medium");
  const [open, setOpen] = useState(false);
  const selectedRef = useRef<HTMLDivElement>(null);
  const operation = useRef(0);
  useEffect(() => () => { operation.current++; }, [sessionId]);

  useEffect(() => {
    if (!config) return;
    setEffort(config.swarm_reasoning_effort || "medium");
  }, [config]);

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
    <div className="relative shrink-0" data-testid="swarm-reasoning-picker">
      <MenuRoot open={open} onOpenChange={setOpen} onOpenChangeComplete={(next) => { if (next) selectedRef.current?.focus(); }}>
        <Menu.Trigger
          aria-label={`Worker reasoning: ${labelForEffort(effort)}`}
          title={`Worker reasoning effort: ${labelForEffort(effort)}. Applies to swarm and implement workers, not to the chat pilot.`}
          className="composer-control flex items-center gap-1 text-ui-11 text-muted hover:text-txt rounded-md px-2 bg-transparent hover:bg-panel2 border border-edge/40 transition-colors select-none"
        >
          <Users size={11} className="composer-toolbar-icon shrink-0 text-faint" aria-hidden="true" />
          <span className="composer-toolbar-label text-faint">Workers</span>
          <span className="truncate max-w-[72px]">{labelForEffort(effort)}</span>
          <ChevronDown size={11} className="shrink-0 opacity-60" />
        </Menu.Trigger>
        <MenuPanel
          side="top"
          label="Worker reasoning picker"
          className="min-w-[140px] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden outline-none"
        >
          <ReasoningLevelOptions value={effort} onSelect={setWorkerEffort} selectedRef={selectedRef} />
        </MenuPanel>
      </MenuRoot>
    </div>
  );
}
