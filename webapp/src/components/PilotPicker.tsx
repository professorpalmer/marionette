import { useEffect, useMemo, useState, useRef } from "react";
import { ChevronDown, Check, Search } from "lucide-react";
import { api, type Config, type ReasoningEffort } from "../lib/api";
import { modelLabelOf, organizePilotModels, providerLabelOf } from "../lib/pilotPickerModels";
import { REASONING_LEVELS, labelForEffort, showReasoningEffort } from "../lib/reasoningSupport";
import { useOverlayFocus } from "../lib/overlayFocus";

export default function PilotPicker({ config, sessionId = "" }: {
  config: Config | null;
  sessionId?: string;
}) {
  const [models, setModels] = useState<string[]>([]);
  const [current, setCurrent] = useState("");
  const [reasoning, setReasoning] = useState<ReasoningEffort>("low");
  const [modelOpen, setModelOpen] = useState(false);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [rerouteNotice, setRerouteNotice] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const modelMenuRef = useRef<HTMLDivElement>(null);
  const reasonMenuRef = useRef<HTMLDivElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);
  const modelTriggerRef = useRef<HTMLButtonElement>(null);
  const operation = useRef(0);
  const reasoningOperation = useRef(0);
  useEffect(() => () => { operation.current++; reasoningOperation.current++; }, [sessionId]);

  useOverlayFocus(modelOpen, modelMenuRef, {
    initialFocusRef: filterRef,
    onClose: () => setModelOpen(false),
  });
  useOverlayFocus(reasonOpen, reasonMenuRef, {
    onClose: () => setReasonOpen(false),
  });

  useEffect(() => {
    if (!config) return;
    const nextModels = config.models?.length ? config.models : [config.driver].filter(Boolean);
    setModels(nextModels);
    setReasoning(config.reasoning_effort || "low");
    setCurrent(config.driver);
    if (config.driver && !nextModels.includes(config.driver)) {
      const configuredLabel = modelLabelOf(config.driver, config.model_labels) || config.driver;
      const notice = `Configured pilot ${configuredLabel} is unavailable. Select a model to change it.`;
      setRerouteNotice(notice);
    } else {
      setRerouteNotice(null);
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
    document.addEventListener("mousedown", handleOutsideClick);
    return () => document.removeEventListener("mousedown", handleOutsideClick);
  }, [modelOpen, reasonOpen]);

  const swap = async (m: string) => {
    if (!sessionId) return;
    const generation = ++operation.current;
    const prev = current;
    setCurrent(m);
    setModelOpen(false);
    setRerouteNotice(null);
    try {
      await api.swapPilot(m, sessionId);
      if (generation !== operation.current) return;
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch {
      if (generation !== operation.current) return;
      setCurrent(prev);
      window.dispatchEvent(new CustomEvent("harness-toast", {
        detail: "Model switch failed -- try again",
      }));
    }
  };

  const setReasoningEffort = async (level: ReasoningEffort) => {
    if (!sessionId) return;
    const generation = ++reasoningOperation.current;
    const prev = reasoning;
    setReasoning(level);
    setReasonOpen(false);
    try {
      await api.setPilotPreferences(sessionId, { reasoning_effort: level });
      if (generation !== reasoningOperation.current) return;
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch {
      if (generation !== reasoningOperation.current) return;
      setReasoning(prev);
      window.dispatchEvent(new CustomEvent("harness-toast", {
        detail: "Reasoning setting failed -- try again",
      }));
    }
  };

  const labels = config?.model_labels;
  const shortOf = (spec: string) => (spec ? spec.split(":").pop() || "" : "");
  const shortCounts = models.reduce<Record<string, number>>((acc, m) => {
    const s = shortOf(m);
    if (s) acc[s] = (acc[s] || 0) + 1;
    return acc;
  }, {});
  const labelOf = (spec: string) => {
    const short = shortOf(spec);
    if (!short) return "";
    const friendly = modelLabelOf(spec, labels);
    if ((shortCounts[short] || 0) > 1 && spec.includes(":")) {
      return `${friendly} (${spec.split(":")[0]})`;
    }
    return friendly;
  };

  const organized = useMemo(
    () => organizePilotModels(models, current, query, labels),
    [models, current, query, labels],
  );

  if (!config) return null;

  const currentLabel = labelOf(current);
  const showReasoning = showReasoningEffort(config?.reasoning_support, current);
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
        <span className="min-w-0 flex-1 leading-snug" title={m}>{label}</span>
        {isSelected && <Check size={11} className="shrink-0 ml-2" />}
      </div>
    );
  };

  return (
    <div className="relative inline-flex flex-col items-stretch gap-0.5 min-w-0" ref={containerRef}>
      {rerouteNotice ? (
        <div
          role="status"
          className="text-[9.5px] text-warn/90 leading-snug px-0.5"
          data-testid="pilot-reroute-notice"
        >
          {rerouteNotice}
        </div>
      ) : null}
      <div className="pilot-picker-controls relative inline-flex items-center gap-1 min-w-0">
      <div className="pilot-model-slot relative min-w-0">
        <button
          ref={modelTriggerRef}
          onClick={() => {
            setReasonOpen(false);
            setModelOpen((prev) => !prev);
          }}
          title={current || "Pilot model"}
          className="flex items-center gap-1 min-w-0 text-[11px] text-muted hover:text-txt rounded-md px-2 h-[22px] bg-transparent hover:bg-panel2 border border-edge/40 transition select-none"
        >
          <span className="pilot-picker-trigger-label text-left">{currentLabel}</span>
          <ChevronDown size={11} className="shrink-0 opacity-60" />
        </button>

        {modelOpen && (
          <div
            ref={modelMenuRef}
            role="dialog"
            aria-modal="true"
            aria-label="Pilot model picker"
            className="absolute left-0 bottom-full mb-1 z-50 w-[min(20rem,calc(100vw-2rem))] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden"
          >
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
                      <div className="px-3 pt-1.5 pb-0.5 text-[10px] text-faint font-medium select-none">
                        {providerLabelOf(g.provider)}
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
        <div className="relative shrink-0">
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
            <div
              ref={reasonMenuRef}
              role="dialog"
              aria-modal="true"
              aria-label="Reasoning effort picker"
              className="absolute left-0 bottom-full mb-1 z-50 min-w-[140px] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden"
            >
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
    </div>
  );
}
