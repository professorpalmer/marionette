import { useEffect, useMemo, useState, useRef } from "react";
import { ChevronDown, Check, Search } from "lucide-react";
import { api, type Config, type ReasoningEffort } from "../lib/api";
import { organizePilotModels } from "../lib/pilotPickerModels";

const REASONING_LEVELS: { value: ReasoningEffort; label: string }[] = [
  { value: "none", label: "None" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "Extra High" },
  { value: "max", label: "Max" },
];

const CODEX_REACHES = new Set(["openai-codex", "codex-plan", "chatgpt-codex"]);

/** Effort picker: Codex always; Anthropic/Bedrock Claude opus|sonnet only. */
function supportsReasoningEffort(driver: string): boolean {
  const reach = (driver.split(":")[0] || "").toLowerCase();
  const model = (driver.split(":")[1] || "").toLowerCase();
  if (CODEX_REACHES.has(reach)) return true;
  if (reach === "anthropic" || reach === "bedrock") {
    if (model.includes("haiku")) return false;
    return model.includes("opus") || model.includes("sonnet");
  }
  return false;
}

function labelForEffort(value: ReasoningEffort): string {
  return REASONING_LEVELS.find((l) => l.value === value)?.label || "Low";
}

export default function PilotPicker({ config }: {
  config: Config | null;
}) {
  const [models, setModels] = useState<string[]>([]);
  const [current, setCurrent] = useState("");
  const [reasoning, setReasoning] = useState<ReasoningEffort>("low");
  const [modelOpen, setModelOpen] = useState(false);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [query, setQuery] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (config) {
      setModels(config.models || [config.driver]);
      setCurrent(config.driver);
      setReasoning(config.reasoning_effort || "low");
    }
  }, [config]);

  useEffect(() => {
    const handleOpen = () => setModelOpen(true);
    window.addEventListener("harness-open-model-picker", handleOpen);
    return () => window.removeEventListener("harness-open-model-picker", handleOpen);
  }, []);

  useEffect(() => {
    if (!modelOpen) {
      setQuery("");
      return;
    }
    // Focus search when the model menu opens.
    const t = window.setTimeout(() => filterRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [modelOpen]);

  useEffect(() => {
    if (!modelOpen && !reasonOpen) return;
    const handleOutsideClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setModelOpen(false);
        setReasonOpen(false);
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setModelOpen(false);
        setReasonOpen(false);
      }
    };
    document.addEventListener("mousedown", handleOutsideClick);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [modelOpen, reasonOpen]);

  const swap = async (m: string) => {
    const prev = current;
    setCurrent(m);
    setModelOpen(false);
    try {
      await api.swapPilot(m);
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch {
      setCurrent(prev);
      window.dispatchEvent(new CustomEvent("harness-toast", {
        detail: "Model switch failed -- try again",
      }));
    }
  };

  const setReasoningEffort = async (level: ReasoningEffort) => {
    const prev = reasoning;
    setReasoning(level);
    setReasonOpen(false);
    try {
      await api.updateSettings({ reasoning_effort: level });
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch {
      setReasoning(prev);
      window.dispatchEvent(new CustomEvent("harness-toast", {
        detail: "Reasoning setting failed -- try again",
      }));
    }
  };

  const shortOf = (spec: string) => (spec ? spec.split(":").pop() || "" : "");
  const shortCounts = models.reduce<Record<string, number>>((acc, m) => {
    const s = shortOf(m);
    if (s) acc[s] = (acc[s] || 0) + 1;
    return acc;
  }, {});
  const labelOf = (spec: string) => {
    const short = shortOf(spec);
    if (!short) return "";
    if ((shortCounts[short] || 0) > 1 && spec.includes(":")) {
      return `${short} (${spec.split(":")[0]})`;
    }
    return short;
  };

  const organized = useMemo(
    () => organizePilotModels(models, current, query),
    [models, current, query],
  );

  if (!config) return null;

  const currentLabel = labelOf(current);
  const showReasoning = supportsReasoningEffort(current);
  const hasRows = !!organized.current || organized.groups.some((g) => g.items.length > 0);

  const renderRow = (m: string) => {
    const isSelected = m === current;
    const label = labelOf(m);
    return (
      <div
        key={m}
        onClick={() => swap(m)}
        className={`flex items-center justify-between px-3 py-1.5 text-[11.5px] hover:bg-panel2 cursor-pointer transition select-none ${
          isSelected ? "text-accent font-medium bg-panel2/40" : "text-txt/90"
        }`}
      >
        <span className="truncate max-w-[200px]" title={m}>{label}</span>
        {isSelected && <Check size={11} className="shrink-0 ml-2" />}
      </div>
    );
  };

  return (
    <div className="relative inline-flex items-center gap-1" ref={containerRef}>
      <div className="relative inline-block">
        <button
          onClick={() => {
            setReasonOpen(false);
            setModelOpen((prev) => !prev);
          }}
          title={current || "Pilot model"}
          className="flex items-center gap-1 text-[11px] text-muted hover:text-txt rounded-md px-2 h-[22px] bg-transparent hover:bg-panel2 border border-edge/40 transition select-none"
        >
          <span className="truncate max-w-[170px]">{currentLabel}</span>
          <ChevronDown size={11} className="shrink-0 opacity-60" />
        </button>

        {modelOpen && (
          <div className="absolute left-0 bottom-full mb-1 z-50 min-w-[220px] max-w-[280px] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden">
            <div className="flex items-center gap-1.5 px-2.5 py-1.5 mb-0.5 border-b border-edge/50">
              <Search size={12} className="text-faint shrink-0" />
              <input
                ref={filterRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.stopPropagation()}
                placeholder="Search models or providers"
                className="bg-transparent text-[11.5px] text-txt placeholder:text-faint outline-none w-full"
              />
            </div>
            <div className="max-h-[280px] overflow-y-auto">
              {!hasRows ? (
                <div className="px-3 py-2 text-[11px] text-faint">No matching models</div>
              ) : (
                <>
                  {organized.current && renderRow(organized.current)}
                  {organized.groups.map((g) => (
                    <div key={g.provider}>
                      <div className="px-3 pt-1.5 pb-0.5 text-[9.5px] uppercase tracking-wider text-faint font-semibold select-none">
                        {g.provider}
                      </div>
                      {g.items.map((m) => renderRow(m))}
                    </div>
                  ))}
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {showReasoning && (
        <div className="relative inline-block">
          <button
            onClick={() => {
              setModelOpen(false);
              setReasonOpen((prev) => !prev);
            }}
            title={`Reasoning effort (${labelForEffort(reasoning)})`}
            className="flex items-center gap-1 text-[11px] text-muted hover:text-txt rounded-md px-2 h-[22px] bg-transparent hover:bg-panel2 border border-edge/40 transition select-none"
          >
            <span className="truncate max-w-[90px]">{labelForEffort(reasoning)}</span>
            <ChevronDown size={11} className="shrink-0 opacity-60" />
          </button>

          {reasonOpen && (
            <div className="absolute left-0 bottom-full mb-1 z-50 min-w-[140px] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden">
              {REASONING_LEVELS.map(({ value, label }) => {
                const isSelected = value === reasoning;
                return (
                  <div
                    key={value}
                    onClick={() => setReasoningEffort(value)}
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
      )}
    </div>
  );
}
