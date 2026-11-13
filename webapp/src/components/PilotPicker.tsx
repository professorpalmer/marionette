import { useEffect, useId, useMemo, useState, useRef } from "react";
import { Menu } from "@base-ui/react/menu";
import { Popover } from "@base-ui/react/popover";
import { Brain, ChevronDown, Check, Loader2, Search } from "lucide-react";
import { api, type Config, type ReasoningEffort } from "../lib/api";
import { modelLabelOf, organizePilotModels, providerLabelOf } from "../lib/pilotPickerModels";
import { labelForEffort, showReasoningEffort } from "../lib/reasoningSupport";
import ReasoningLevelOptions from "./ReasoningLevelOptions";
import { MenuPanel, MenuRoot } from "./ui/overlay";
import { loadModelFacts, modelFactParts, type ModelFactsState } from "../lib/pilotModelFacts";
import { getSessionCache, updateSessionCache } from "../lib/sessionCache";

type PickerSeed = {
  models: string[];
  current: string;
  reasoning: ReasoningEffort;
  rerouteNotice: string | null;
};

function seedFromConfig(
  config: Config,
  sessionId: string,
  pendingModel: string | undefined,
  pendingReasoning: ReasoningEffort | undefined,
): PickerSeed {
  const models = config.models?.length ? config.models : [config.driver].filter(Boolean);
  const unavailable = Boolean(config.driver) && !models.includes(config.driver);
  const configuredLabel = unavailable ? modelLabelOf(config.driver, config.model_labels) || config.driver : "";
  return {
    models,
    current: !sessionId && pendingModel ? pendingModel : config.driver,
    reasoning: !sessionId && pendingReasoning ? pendingReasoning : config.reasoning_effort || "low",
    rerouteNotice: unavailable
      ? `Configured pilot ${configuredLabel} is unavailable. Select a model to change it.`
      : null,
  };
}

export default function PilotPicker({
  config,
  sessionId = "",
  pendingModel,
  pendingReasoning,
  setupNotice,
  modelSelectionDisabled = false,
  onPendingModelChange,
  onPendingReasoningChange,
  onSessionModelChange,
}: {
  config: Config | null;
  sessionId?: string;
  pendingModel?: string;
  pendingReasoning?: ReasoningEffort;
  setupNotice?: string;
  modelSelectionDisabled?: boolean;
  onPendingModelChange?: (model: string) => void;
  onPendingReasoningChange?: (level: ReasoningEffort, model: string) => void;
  onSessionModelChange?: (sessionId: string, model: string) => Promise<unknown>;
}) {
  // Remounts on session switch (keyed by sessionId): seed the first paint from
  // config so the trigger never paints empty and then widens.
  const [seed] = useState(() => (config ? seedFromConfig(config, sessionId, pendingModel, pendingReasoning) : null));
  const [models, setModels] = useState<string[]>(seed?.models ?? []);
  const [current, setCurrent] = useState(seed?.current ?? "");
  const [reasoning, setReasoning] = useState<ReasoningEffort>(seed?.reasoning ?? "low");
  const [modelOpen, setModelOpen] = useState(false);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const optionIdBase = useId();
  const [rerouteNotice, setRerouteNotice] = useState<string | null>(seed?.rerouteNotice ?? null);
  const reasonSelectedRef = useRef<HTMLDivElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);
  const operation = useRef(0);
  const reasoningOperation = useRef(0);
  const retainOperation = useRef(0);
  const [factsState, setFactsState] = useState<ModelFactsState>({ kind: "loading" });
  const [retainReasoning, setRetainReasoning] = useState(false);
  const [retainReady, setRetainReady] = useState(false);
  const [savingRetain, setSavingRetain] = useState(false);
  useEffect(() => () => { operation.current++; reasoningOperation.current++; retainOperation.current++; }, [sessionId]);

  useEffect(() => {
    if (!config) return;
    const next = seedFromConfig(config, sessionId, pendingModel, pendingReasoning);
    setModels(next.models);
    setReasoning(next.reasoning);
    setCurrent(next.current);
    setRerouteNotice(next.rerouteNotice);
  }, [config, pendingModel, pendingReasoning, sessionId]);

  useEffect(() => {
    const handleOpen = () => setModelOpen(true);
    window.addEventListener("harness-open-model-picker", handleOpen);
    return () => window.removeEventListener("harness-open-model-picker", handleOpen);
  }, []);

  useEffect(() => {
    if (!modelOpen) return;
    let active = true;
    loadModelFacts().then(
      (facts) => { if (active) setFactsState({ kind: "ready", facts }); },
      () => { if (active) setFactsState({ kind: "unavailable" }); },
    );
    return () => { active = false; };
  }, [modelOpen]);

  useEffect(() => {
    if (!modelOpen) setQuery("");
  }, [modelOpen]);

  useEffect(() => {
    if (!sessionId || !reasonOpen || reasoning === "none") return;
    let active = true;
    const generation = ++retainOperation.current;
    void getSessionCache(sessionId).then((cache) => {
      if (!active || generation !== retainOperation.current) return;
      setRetainReasoning(cache.retain_reasoning);
      setRetainReady(true);
    }).catch(() => {
      if (!active || generation !== retainOperation.current) return;
      setRetainReady(false);
    });
    return () => { active = false; };
  }, [sessionId, reasonOpen, reasoning]);

  const swap = async (m: string) => {
    if (modelSelectionDisabled) return;
    if (!sessionId) {
      setCurrent(m);
      setModelOpen(false);
      setRerouteNotice(null);
      onPendingModelChange?.(m);
      return;
    }
    const generation = ++operation.current;
    const prev = current;
    setCurrent(m);
    setModelOpen(false);
    setRerouteNotice(null);
    try {
      if (onSessionModelChange) await onSessionModelChange(sessionId, m);
      else await api.swapPilot(m, sessionId);
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

  const setRetainThinking = async (enabled: boolean) => {
    if (!sessionId) return;
    const generation = ++retainOperation.current;
    const prev = retainReasoning;
    setRetainReasoning(enabled);
    setSavingRetain(true);
    try {
      const cache = await updateSessionCache(sessionId, { action: "preferences", retain_reasoning: enabled });
      if (generation !== retainOperation.current) return;
      setRetainReasoning(cache.retain_reasoning);
      setRetainReady(true);
    } catch {
      if (generation !== retainOperation.current) return;
      setRetainReasoning(prev);
      window.dispatchEvent(new CustomEvent("harness-toast", {
        detail: "Could not update thinking retention. Finish the turn and try again.",
      }));
    } finally {
      if (generation === retainOperation.current) setSavingRetain(false);
    }
  };

  const setReasoningEffort = async (level: ReasoningEffort) => {
    if (!sessionId) {
      // No session yet: stage the level with the pending model; App applies
      // both when the first send creates the session.
      setReasoning(level);
      setReasonOpen(false);
      onPendingReasoningChange?.(level, current);
      return;
    }
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
  // Keyboard order matches paint order: current model first, then groups.
  const flat = useMemo(
    () => [...(organized.current ? [organized.current] : []), ...organized.groups.flatMap((g) => g.items)],
    [organized],
  );
  const activeModel = flat[Math.min(activeIdx, flat.length - 1)];
  useEffect(() => {
    if (modelOpen) setActiveIdx(0);
  }, [modelOpen, query]);
  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView?.({ block: "nearest" });
  }, [activeModel]);
  const optionId = (m: string) => `${optionIdBase}-${flat.indexOf(m)}`;

  if (!config) return null;

  // Workspace-only config: this session's driver has not landed yet. Stay
  // clickable so a lagging view can never strand the user without a picker.
  const pending = !config.driver;
  const currentLabel = pending ? "Select model" : labelOf(current);
  const showReasoning = !pending && showReasoningEffort(config?.reasoning_support, current);
  const hasRows = !!organized.current || organized.groups.some((g) => g.items.length > 0);
  // Selection is disabled only while App binds the chosen pilot to the session.
  const binding = modelSelectionDisabled && Boolean(setupNotice);
  const floatingNotice = binding ? rerouteNotice : setupNotice || rerouteNotice;

  const renderRow = (m: string) => {
    const isSelected = m === current;
    const isActive = m === activeModel;
    const label = labelOf(m);
    const facts = factsState.kind === "ready" ? factsState.facts.get(m) : undefined;
    // The line keeps its height while loading so rows never jump.
    const meta = factsState.kind === "loading"
      ? "\u00a0"
      : facts
        ? modelFactParts(facts).join(" \u00b7 ")
        : "Details unknown";
    return (
      <div
        key={m}
        id={optionId(m)}
        role="option"
        aria-selected={isSelected}
        aria-description={factsState.kind === "loading" ? undefined : meta}
        data-active={isActive ? "true" : undefined}
        onClick={() => swap(m)}
        onMouseMove={() => { if (!isActive) setActiveIdx(flat.indexOf(m)); }}
        className={`flex flex-col px-3 py-1.5 text-ui-11.5 cursor-pointer transition-colors select-none ${
          isActive ? "bg-panel2" : ""
        } ${isSelected ? "text-accent font-medium" : "text-txt/90"}`}
        title={m}
      >
        <span className="flex items-center justify-between min-w-0">
          <span className="leading-snug truncate">{label}</span>
          {isSelected && <Check size={11} className="shrink-0 ml-2" />}
        </span>
        <span className="text-ui-10 text-faint font-normal tabular-nums leading-snug truncate" data-testid="pilot-model-facts">{meta}</span>
      </div>
    );
  };

  return (
    <div className="relative inline-flex min-w-0">
      {/* Notices float above the picker so the toolbar height never changes.
          Binding progress lives in the trigger; this region announces it. */}
      <span role="status" className="sr-only">{binding ? setupNotice : ""}</span>
      {floatingNotice ? (
        <div
          role="status"
          className="absolute left-0 bottom-full mb-1 z-40 w-max max-w-[min(20rem,calc(100vw-2rem))] rounded-md bg-panel border border-warn/30 px-2 py-1 text-ui-9.5 text-warn/90 leading-snug shadow-lg"
          data-testid="pilot-reroute-notice"
        >
          {floatingNotice}
        </div>
      ) : null}
      <div className="pilot-picker-controls relative inline-flex items-center gap-1 min-w-0">
      <div className="pilot-model-slot relative min-w-0">
        <Popover.Root
          open={modelOpen}
          modal="trap-focus"
          onOpenChange={(next) => {
            if (next) setReasonOpen(false);
            setModelOpen(next);
          }}
        >
          <Popover.Trigger
            disabled={modelSelectionDisabled}
            title={current || "Pilot model"}
            className="composer-control flex items-center gap-1 min-w-0 text-ui-11 text-muted hover:text-txt rounded-md px-2 bg-transparent hover:bg-panel2 border border-edge/40 transition-colors select-none"
          >
            <span className="pilot-picker-trigger-label text-left">{currentLabel}</span>
            {binding
              ? <Loader2 size={11} className="shrink-0 opacity-60 animate-spin" aria-hidden="true" />
              : <ChevronDown size={11} className="shrink-0 opacity-60" />}
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Positioner className="z-50 outline-none" side="top" align="start" sideOffset={4}>
              <Popover.Popup
                aria-label="Pilot model picker"
                initialFocus={filterRef}
                className="w-[min(23rem,calc(100vw-2rem))] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden outline-none"
              >
                <div className="flex items-center gap-1.5 px-2.5 py-1.5 mb-0.5 border-b border-edge/50">
                  <Search size={12} className="text-faint shrink-0" />
                  <input
                    ref={filterRef}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={(e) => {
                      e.stopPropagation();
                      if (e.nativeEvent.isComposing) return;
                      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                        e.preventDefault();
                        if (!flat.length) return;
                        const step = e.key === "ArrowDown" ? 1 : -1;
                        setActiveIdx((i) => (Math.min(i, flat.length - 1) + step + flat.length) % flat.length);
                      } else if (e.key === "Enter" && activeModel) {
                        e.preventDefault();
                        void swap(activeModel);
                      }
                    }}
                    role="combobox"
                    aria-expanded="true"
                    aria-controls={`${optionIdBase}-list`}
                    aria-activedescendant={activeModel ? optionId(activeModel) : undefined}
                    aria-label="Search models or providers"
                    placeholder="Search models or providers"
                    className="bg-transparent text-ui-11.5 text-txt placeholder:text-faint outline-none w-full"
                  />
                </div>
                <div ref={listRef} id={`${optionIdBase}-list`} role="listbox" aria-label="Models" className="max-h-[340px] overflow-y-auto">
                  {!hasRows ? (
                    <div className="px-3 py-2 text-ui-11 text-faint">No matching models</div>
                  ) : (
                    <>
                      {organized.current && renderRow(organized.current)}
                      {organized.groups.map((g) => (
                        <div key={g.provider} role="group" aria-label={providerLabelOf(g.provider)}>
                          <div aria-hidden="true" className="px-3 pt-1.5 pb-0.5 text-ui-10 text-faint font-medium select-none">
                            {providerLabelOf(g.provider)}
                          </div>
                          {g.items.map((m) => renderRow(m))}
                        </div>
                      ))}
                    </>
                  )}
                </div>
              </Popover.Popup>
            </Popover.Positioner>
          </Popover.Portal>
        </Popover.Root>
      </div>

      {showReasoning && (
        <div className="relative shrink-0">
          <MenuRoot
            open={reasonOpen}
            onOpenChange={(next) => {
              if (next) setModelOpen(false);
              setReasonOpen(next);
            }}
            onOpenChangeComplete={(next) => { if (next) reasonSelectedRef.current?.focus(); }}
          >
            <Menu.Trigger
              aria-label={`Pilot reasoning: ${labelForEffort(reasoning)}`}
              title={`Pilot reasoning effort: ${labelForEffort(reasoning)}. Applies to the chat pilot, not to workers.`}
              className="composer-control flex items-center gap-1 text-ui-11 text-muted hover:text-txt rounded-md px-2 bg-transparent hover:bg-panel2 border border-edge/40 transition-colors select-none"
            >
              <Brain size={11} className="composer-toolbar-icon shrink-0 text-faint" aria-hidden="true" />
              <span className="composer-toolbar-label text-faint">Reasoning</span>
              <span className="truncate max-w-[90px]">{labelForEffort(reasoning)}</span>
              <ChevronDown size={11} className="shrink-0 opacity-60" />
            </Menu.Trigger>
            <MenuPanel
              side="top"
              label="Reasoning effort picker"
              className="min-w-[140px] bg-panel border border-edge rounded-lg shadow-lg py-1 overflow-hidden outline-none"
            >
              <ReasoningLevelOptions value={reasoning} onSelect={setReasoningEffort} selectedRef={reasonSelectedRef} />
              {sessionId && retainReady && reasoning !== "none" ? (
                <Menu.CheckboxItem
                  checked={retainReasoning}
                  disabled={savingRetain}
                  onCheckedChange={(checked) => { void setRetainThinking(checked); }}
                  className="flex items-center gap-2 px-3 py-1.5 text-ui-11.5 text-txt/90 border-t border-edge/50 select-none cursor-pointer outline-none data-[highlighted]:bg-panel2 data-[disabled]:opacity-50"
                  title="Off by default. Retains signed or encrypted provider output in this session for cache reuse. Replay stays with the same provider and model."
                >
                  <span
                    aria-hidden="true"
                    className={`flex h-3 w-3 shrink-0 items-center justify-center rounded-sm border ${retainReasoning ? "border-accent bg-accent text-bg" : "border-edge2"}`}
                  >
                    <Menu.CheckboxItemIndicator><Check size={9} strokeWidth={3} /></Menu.CheckboxItemIndicator>
                  </span>
                  Keep thinking blocks
                </Menu.CheckboxItem>
              ) : null}
            </MenuPanel>
          </MenuRoot>
        </div>
      )}
      </div>
    </div>
  );
}
