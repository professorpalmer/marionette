import { useEffect, useState, useRef } from "react";
import { ChevronDown, Check } from "lucide-react";
import { api, type Config, type ReasoningEffort } from "../lib/api";

const REASONING_LEVELS: { value: ReasoningEffort; label: string }[] = [
  { value: "none", label: "None" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "Extra High" },
  { value: "max", label: "Max" },
];

const CODEX_REACHES = new Set(["openai-codex", "codex-plan", "chatgpt-codex"]);

function isCodexPilot(driver: string): boolean {
  const reach = (driver.split(":")[0] || "").toLowerCase();
  return CODEX_REACHES.has(reach);
}

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
  const containerRef = useRef<HTMLDivElement>(null);

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

  if (!config) return null;

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
  const currentLabel = labelOf(current);
  const showReasoning = supportsReasoningEffort(current);

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
          <div className="absolute left-0 bottom-full mb-1 z-50 min-w-[180px] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden">
            {models.map((m) => {
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
            })}
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
