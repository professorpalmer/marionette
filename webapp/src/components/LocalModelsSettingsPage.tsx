import { useEffect, useRef, useState, type ReactNode } from "react";
import { AlertCircle } from "lucide-react";
import {
  api,
  parseLocalToolCalling,
  type LocalCatalogModel,
  type LocalExternalEndpoint,
  type LocalModelCommand,
  type LocalModelProbeResult,
  type LocalModelsSnapshot,
  type LocalToolCallingStatus,
} from "../lib/api";
import { isRecord, isLocalModelsSnapshot, parseIdleMinutes, residencyLabel } from "../lib/localModelParsing";
import ExternalSamplingEditor from "./ExternalSamplingEditor";
import {
  SETTINGS_PRIMARY_BUTTON,
  SETTINGS_QUIET_BUTTON,
  SettingsGroup,
  SettingsPage,
  SettingsRow,
} from "./SettingsLayout";

const FIELD = "mt-0.5 block w-full px-2 py-1 rounded bg-panel border border-edge text-ui-11 text-txt outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent";
const LABEL = "block text-ui-10 text-muted";

const TOOL_CALLING_LABELS: Record<LocalToolCallingStatus, string> = {
  unverified: "Unverified",
  verified: "Verified",
  unsupported: "Unsupported",
  error: "Error",
};

function formatBytes(value?: number | null): string {
  if (!value || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function statusLabel(status: string): string {
  if (status === "ready") return "Installed";
  if (status === "downloading") return "Downloading";
  if (status === "extracting") return "Extracting";
  if (status === "error") return "Error";
  if (status === "paused") return "Paused";
  if (status === "absent") return "Not installed";
  return status || "Unknown";
}

function isProbeResult(value: unknown): value is LocalModelProbeResult {
  if (!isRecord(value)) return false;
  if (!("vendor" in value) || !("models" in value)) return false;
  return typeof value.vendor === "string" && Array.isArray(value.models);
}

function catalogModels(snapshot: LocalModelsSnapshot | null): LocalCatalogModel[] {
  const listed = snapshot?.catalog.models;
  if (listed && listed.length > 0) return listed;
  const one = snapshot?.catalog.model;
  if (one?.id) {
    return [{
      id: one.id,
      name: one.name || one.id,
      quant: one.quant,
      size: one.size,
      context_length: one.context_length,
    }];
  }
  return [];
}

function snapshotCursor(value: LocalModelsSnapshot | null | undefined): number {
  const cursor = value?.event_cursor;
  return typeof cursor === "number" && Number.isFinite(cursor) ? cursor : 0;
}

function shouldAcceptSnapshot(
  incoming: LocalModelsSnapshot,
  ownedCursor: number,
  opts: { source: "get" | "live" | "command"; live: boolean },
): boolean {
  if (opts.source === "get" && opts.live) return false;
  return snapshotCursor(incoming) >= ownedCursor;
}

function backgroundInstallError(snapshot: LocalModelsSnapshot | null | undefined): string {
  const runtimeError = snapshot?.managed?.runtime?.error;
  const modelError = snapshot?.managed?.model?.error;
  const text = [runtimeError, modelError].find((item) => typeof item === "string" && item.trim());
  return text ? String(text) : "";
}

export default function LocalModelsSettingsPage() {
  const [snapshot, setSnapshot] = useState<LocalModelsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [url, setUrl] = useState("http://127.0.0.1:11434/v1");
  const [apiKey, setApiKey] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [contextTokens, setContextTokens] = useState("");
  const [cardContext, setCardContext] = useState<Record<string, string>>({});
  const [acceptLan, setAcceptLan] = useState(false);
  const [acceptRemote, setAcceptRemote] = useState(false);
  const [probe, setProbe] = useState<LocalModelProbeResult | null>(null);
  const [selectedDiscovered, setSelectedDiscovered] = useState("");
  const [selectedCatalogId, setSelectedCatalogId] = useState("");
  const [idleTimeout, setIdleTimeout] = useState("0");
  const [openEditors, setOpenEditors] = useState<Record<string, boolean>>({});
  const idleEdited = useRef(false);
  const idleEditRevision = useRef(0);
  const latestSnapshot = useRef<LocalModelsSnapshot | null>(null);
  const liveSnapshot = useRef(false);
  const eventCursorRef = useRef(0);
  const pollStarted = useRef(false);
  const loadErrorRef = useRef(false);

  const acceptSnapshot = (
    next: unknown,
    source: "get" | "live" | "command",
  ): boolean => {
    if (!isLocalModelsSnapshot(next)) throw new Error("Invalid local model snapshot");
    if (!shouldAcceptSnapshot(next, eventCursorRef.current, {
      source,
      live: liveSnapshot.current,
    })) {
      return false;
    }
    eventCursorRef.current = Math.max(eventCursorRef.current, snapshotCursor(next));
    if (!idleEdited.current) {
      setIdleTimeout(String(next.managed.idle_timeout_minutes));
    }
    if (source !== "get") liveSnapshot.current = true;
    latestSnapshot.current = next;
    setSnapshot(next);
    setLoading(false);
    if (source === "live" && loadErrorRef.current) {
      loadErrorRef.current = false;
      setError("");
    }
    return true;
  };

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const next = await api.getLocalModels();
        if (cancelled) return;
        if (acceptSnapshot(next, "get")) {
          const models = isLocalModelsSnapshot(next) ? catalogModels(next) : [];
          if (models[0]?.id) setSelectedCatalogId(models[0].id);
        }
      } catch (err) {
        if (!cancelled) {
          loadErrorRef.current = true;
          setError(err instanceof Error ? err.message : "Could not load local models");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let stopWatch: (() => void) | undefined;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    let since = 0;

    const startPollFallback = () => {
      if (cancelled || pollStarted.current) return;
      pollStarted.current = true;
      const tick = () => {
        void api.getLocalModelEvents(since).then((res) => {
          if (cancelled) return;
          if (res.snapshot && isLocalModelsSnapshot(res.snapshot)) {
            acceptSnapshot(res.snapshot, "live");
          }
          if (isLocalModelsSnapshot(res.snapshot)) since = Math.max(since, snapshotCursor(res.snapshot));
          pollTimer = window.setTimeout(tick, 4000);
        }).catch(() => {
          if (!cancelled) pollTimer = window.setTimeout(tick, 4000);
        });
      };
      tick();
    };

    stopWatch = api.watchLocalModelEvents({
      since: 0,
      onEvent: (ev) => {
        if (cancelled || !isRecord(ev)) return;
        if (ev.kind === "snapshot" && ev.snapshot && isLocalModelsSnapshot(ev.snapshot)) {
          acceptSnapshot(ev.snapshot, "live");
          since = Math.max(since, snapshotCursor(ev.snapshot));
        }
      },
      onError: () => {
        if (!cancelled) startPollFallback();
      },
      onDone: () => {
        if (!cancelled) startPollFallback();
      },
    });

    return () => {
      cancelled = true;
      stopWatch?.();
      if (pollTimer) window.clearTimeout(pollTimer);
    };
  }, []);

  const run = async (command: LocalModelCommand, label: string) => {
    const submittedIdleRevision = idleEditRevision.current;
    setBusy(label);
    setError("");
    try {
      const result = await api.localModelCommand(command);
      if (isProbeResult(result)) {
        setProbe(result);
        setSelectedDiscovered(result.models[0] || "");
        const probedCtx = result.context_length;
        if (typeof probedCtx === "number" && Number.isFinite(probedCtx) && probedCtx > 0) {
          setContextTokens((prev) => (prev.trim() ? prev : String(probedCtx)));
        }
      } else if (isLocalModelsSnapshot(result)) {
        acceptSnapshot(result, "command");
        if (command.type === "set_policy" && idleEditRevision.current === submittedIdleRevision) {
          idleEdited.current = false;
          setIdleTimeout(String((latestSnapshot.current ?? result).managed.idle_timeout_minutes));
        }
        if (command.type === "save_external") setApiKey("");
        if (command.type === "set_context" || command.type === "save_external") {
          window.dispatchEvent(new Event("harness-context-changed"));
        }
        if (command.type === "activate" || command.type === "start" || command.type === "set_sampling") {
          window.dispatchEvent(new Event("harness-config-changed"));
        }
      } else {
        throw new Error("Invalid local model response");
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Local model command failed";
      setError(message);
    } finally {
      setBusy(null);
    }
  };

  const title = "Local Models";
  const purpose = "Run a model on this machine, or connect a model server that you already run.";

  if (loading && !snapshot) {
    return (
      <SettingsPage title={title} description={purpose} testId="local-models-page">
        <p className="text-ui-12 text-faint">Loading hardware and install state...</p>
      </SettingsPage>
    );
  }

  const hardware = snapshot?.hardware;
  const managed = snapshot?.managed;
  const runtimeStatus = managed?.runtime.status || "absent";
  const modelStatus = managed?.model.status || "absent";
  const alertText = error || backgroundInstallError(snapshot);
  const process = managed?.process;
  const installing = ["downloading", "extracting"].includes(runtimeStatus)
    || ["downloading", "extracting"].includes(modelStatus);
  const downloading = runtimeStatus === "downloading" || modelStatus === "downloading";
  const paused = runtimeStatus === "paused" || modelStatus === "paused";
  const installed = runtimeStatus === "ready" && modelStatus === "ready";
  const running = managed?.residency === "running";
  const lifecycleBusy = managed?.residency === "starting" || managed?.residency === "stopping";
  const idleMinutes = parseIdleMinutes(idleTimeout);
  const download = managed?.downloads?.model || managed?.downloads?.runtime;
  const progress = download?.total
    ? Math.min(100, Math.round(((download.bytes || 0) / download.total) * 100))
    : 0;
  const spec = managed?.spec || "";
  const active = snapshot?.active_spec || "";
  const models = catalogModels(snapshot);
  const selectedModel = models.find((item) => item.id === selectedCatalogId) || models[0] || null;
  const attachModel = (manualModel.trim() || selectedDiscovered).trim();
  const canSave = Boolean(url.trim() && attachModel);
  const modelPresent = modelStatus !== "absent";
  const runtimePresent = runtimeStatus !== "absent";
  const externals = snapshot?.externals || [];
  const off = busy !== null;

  const hardwareLine = [
    hardware?.platform_key || "unknown",
    hardware?.accelerator || "cpu",
    hardware?.ram_bytes ? `${formatBytes(hardware.ram_bytes)} RAM` : "",
    hardware?.disk_free_bytes ? `${formatBytes(hardware.disk_free_bytes)} free` : "",
  ].filter(Boolean).join(" · ");

  const modelFacts = selectedModel ? [
    selectedModel.name || selectedModel.id,
    `Size ${formatBytes(selectedModel.size)}`,
    selectedModel.min_ram_gb ? `RAM ${selectedModel.min_ram_gb} GB` : "",
    selectedModel.min_disk_bytes ? `Disk ${formatBytes(selectedModel.min_disk_bytes)}` : "",
    selectedModel.context_length ? `Context ${selectedModel.context_length}` : "",
    `Source ${selectedModel.source || "catalog"}`,
    `Trust ${selectedModel.trust || "catalog"}`,
  ].filter(Boolean).join(" · ") : "";

  const install = (label: "install" | "resume") => void run({
    type: "install",
    target: "all",
    model_id: selectedModel?.id || "",
  }, label);

  // The one lifecycle action for the current state.
  let lifecycle: ReactNode = null;
  if (downloading) {
    lifecycle = <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={off} onClick={() => void run({ type: "cancel", target: "all" }, "cancel")}>Cancel</button>;
  } else if (paused) {
    lifecycle = <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={off || !hardware?.supported || !selectedModel?.id} onClick={() => install("resume")}>Resume</button>;
  } else if (!installed && !installing) {
    lifecycle = <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={off || !hardware?.supported || !selectedModel?.id} onClick={() => install("install")}>Install</button>;
  } else if (installed && running) {
    lifecycle = (
      <>
        <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={off} onClick={() => void run({ type: "restart" }, "restart")}>Restart</button>
        <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={off} onClick={() => void run({ type: "stop" }, "stop")}>Stop</button>
      </>
    );
  } else if (installed && !lifecycleBusy) {
    lifecycle = <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={off} onClick={() => void run({ type: "start" }, "start")}>Start</button>;
  }

  return (
    <SettingsPage title={title} description={purpose} testId="local-models-page">
      {alertText ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-md border border-edge/50 bg-panel2 px-3 py-2 text-ui-12 text-txt"
          data-testid="local-models-error"
        >
          <AlertCircle size={14} className="mt-0.5 text-faint shrink-0" aria-hidden="true" />
          <span>{alertText}</span>
        </div>
      ) : null}

      <SettingsGroup title="This machine" testId="local-models-hardware">
        <SettingsRow
          label="Hardware"
          description={hardwareLine}
          state={hardware?.supported ? "Managed install available" : "No managed install"}
        >
          {hardware?.supported ? null : (
            <p className="text-ui-11 text-muted" data-testid="local-models-unsupported">
              {hardware?.unsupported_reason || "Managed install is not available on this machine."}
            </p>
          )}
        </SettingsRow>
      </SettingsGroup>

      <SettingsGroup
        title="Managed llama.cpp"
        description="Marionette downloads a pinned llama.cpp runtime and one catalog model, then runs the server."
        testId="local-models-managed"
      >
        <SettingsRow
          testId="local-models-catalog-facts"
          label="Model"
          description={models.length > 0 ? modelFacts : "No catalog model is in this build."}
          control={models.length > 0 ? (
            <span data-testid="local-models-catalog-select">
              <select
                aria-label="Catalog model"
                className="bg-panel border border-edge rounded px-1.5 py-0.5 text-ui-11 text-txt"
                value={selectedModel?.id || ""}
                onChange={(event) => setSelectedCatalogId(event.target.value)}
              >
                {models.map((item) => (
                  <option key={item.id} value={item.id}>{item.name || item.id}</option>
                ))}
              </select>
            </span>
          ) : null}
        />
        <SettingsRow
          testId="local-models-managed-status"
          label="Server"
          description={[
            `Runtime ${statusLabel(runtimeStatus)}`,
            `Model ${statusLabel(modelStatus)}`,
            `Server ${managed ? residencyLabel(managed) : "Status unknown"}`,
            process?.context_length ? `${process.context_length} context` : "",
          ].filter(Boolean).join(" · ")}
          control={lifecycle}
        >
          {managed?.residency === "stopped" && managed.stop_reason === "inactivity" ? (
            <p className="text-ui-11 text-muted">Start to use this model.</p>
          ) : null}
          {download && (installing || paused) ? (
            <div data-testid="local-models-progress">
              <div className="h-1.5 rounded bg-panel overflow-hidden">
                <div className="h-full bg-accent motion-reduce:transition-none" style={{ width: `${progress}%` }} />
              </div>
              <p className="text-ui-11 text-muted mt-1">
                {download.phase || "download"} {"·"} {formatBytes(download.bytes)} / {formatBytes(download.total)}
              </p>
            </div>
          ) : null}
        </SettingsRow>
        {running ? (
          <SettingsRow
            label="Pilot picker"
            description="Make this model the pilot model."
            state={active === spec ? "Active" : "Not active"}
            control={(
              <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={off || !spec} onClick={() => void run({ type: "activate", spec }, "activate")}>
                {active === spec ? "Active in picker" : "Activate"}
              </button>
            )}
          />
        ) : null}
        {installed ? (
          <SettingsRow
            testId="local-models-idle-policy"
            label="Unload after inactivity"
            description="Minutes without a request. 0 keeps the server loaded. Only Marionette requests count. Keep auto-stop off when shared external clients use this server."
            control={(
              <>
                <input
                  aria-label="Unload after inactivity, in minutes"
                  className="w-20 px-2 py-0.5 rounded bg-panel border border-edge text-ui-11 text-txt"
                  type="number"
                  min={0}
                  max={1440}
                  step={1}
                  value={idleTimeout}
                  onChange={(event) => { idleEdited.current = true; idleEditRevision.current += 1; setIdleTimeout(event.target.value); }}
                />
                <button
                  type="button"
                  className={SETTINGS_PRIMARY_BUTTON}
                  disabled={off || lifecycleBusy || idleMinutes === null}
                  onClick={() => {
                    if (idleMinutes !== null) void run({ type: "set_policy", idle_timeout_minutes: idleMinutes }, "policy");
                  }}
                >
                  Apply
                </button>
              </>
            )}
          />
        ) : null}
        {modelPresent || runtimePresent ? (
          <SettingsRow
            label="Downloaded files"
            description="Delete the model file, the runtime, or both."
            control={(
              <>
                {modelPresent ? (
                  <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={off} onClick={() => void run({ type: "remove", target: "model" }, "remove")}>Remove model</button>
                ) : null}
                {runtimePresent ? (
                  <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={off} onClick={() => void run({ type: "remove", target: "runtime" }, "remove")}>Remove runtime</button>
                ) : null}
                {modelPresent && runtimePresent ? (
                  <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={off} onClick={() => void run({ type: "remove", target: "all" }, "remove")}>Remove all</button>
                ) : null}
              </>
            )}
          />
        ) : null}
      </SettingsGroup>

      <SettingsGroup
        title="Saved servers"
        description="Activate a server to use it as the pilot. Test tool calling sends one request that asks for a tool call and records the answer. It does not run tools or enroll the model as a Puppetmaster swarm worker."
        testId="local-models-external"
      >
        {externals.length === 0 ? (
          <SettingsRow
            testId="local-models-empty-external"
            label="No saved servers"
            description="Probe a URL below, or save one with a model id."
          />
        ) : externals.map((endpoint: LocalExternalEndpoint) => {
          const endpointSpec = `local:${endpoint.id}/${endpoint.selected_model || ""}`;
          const toolCalling = parseLocalToolCalling(endpoint.tool_calling);
          const name = endpoint.name || endpoint.id;
          const isActive = active === endpointSpec;
          const editing = !!openEditors[endpoint.id];
          return (
            <SettingsRow
              key={endpoint.id}
              testId={`local-external-${endpoint.id}`}
              label={`${name} · ${endpoint.vendor}`}
              description={endpoint.base_url}
              state={isActive ? "Active" : endpoint.healthy ? "Reachable" : "Saved"}
              control={(
                <button
                  type="button"
                  className={SETTINGS_PRIMARY_BUTTON}
                  disabled={off}
                  onClick={() => void run({ type: "activate", spec: endpointSpec }, "activate")}
                >
                  {isActive ? "Active in picker" : "Activate"}
                </button>
              )}
            >
              <div className="space-y-1.5">
                <p className="text-ui-11 text-muted">
                  <span data-testid={`local-external-context-${endpoint.id}`}>
                    {typeof endpoint.context_length === "number" && endpoint.context_length > 0
                      ? `Context ${endpoint.context_length.toLocaleString()} tokens`
                      : "Context unknown"}
                  </span>
                  {" · "}
                  <span data-testid={`local-external-tool-calling-${endpoint.id}`}>
                    Tool calling {TOOL_CALLING_LABELS[toolCalling.status]}
                    {toolCalling.reason ? `: ${toolCalling.reason}` : ""}
                  </span>
                </p>
                <div className="flex flex-wrap items-center gap-3">
                  <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={off} onClick={() => void run({ type: "verify_tool_calling", spec: endpointSpec }, "verify")}>
                    Test tool calling
                  </button>
                  <button
                    type="button"
                    className={SETTINGS_QUIET_BUTTON}
                    aria-expanded={editing}
                    aria-controls={`local-external-body-${endpoint.id}`}
                    data-testid={`local-external-toggle-${endpoint.id}`}
                    onClick={() => setOpenEditors((prev) => ({ ...prev, [endpoint.id]: !prev[endpoint.id] }))}
                  >
                    {editing ? "Hide context and sampling" : "Context and sampling"}
                  </button>
                  <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={off} onClick={() => void run({ type: "remove", target: "all", endpoint_id: endpoint.id }, "remove")}>
                    Delete
                  </button>
                </div>
                {editing ? (
                  <div id={`local-external-body-${endpoint.id}`} className="space-y-2 border-t border-edge/30 pt-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        type="number"
                        min={1}
                        aria-label={`Context tokens for ${name}`}
                        value={cardContext[endpoint.id] ?? ""}
                        placeholder="262144"
                        onChange={(event) => {
                          const value = event.target.value;
                          setCardContext((prev) => ({ ...prev, [endpoint.id]: value }));
                        }}
                        className="w-28 px-2 py-0.5 rounded bg-panel border border-edge text-ui-11 text-txt"
                      />
                      <button
                        type="button"
                        className={SETTINGS_PRIMARY_BUTTON}
                        disabled={off}
                        onClick={() => {
                          const parsed = Number.parseInt((cardContext[endpoint.id] || "").trim(), 10);
                          if (!Number.isFinite(parsed) || parsed <= 0) {
                            setError("Enter a positive context token count");
                            return;
                          }
                          void run({ type: "set_context", endpoint_id: endpoint.id, context_length: parsed }, "set_context");
                        }}
                      >
                        Set
                      </button>
                      <span className="text-ui-10 text-faint">Context window override, in tokens.</span>
                    </div>
                    <ExternalSamplingEditor
                      endpoint={endpoint}
                      disabled={off}
                      onCommand={(command) => void run(command, "set_sampling")}
                      onError={setError}
                    />
                  </div>
                ) : null}
              </div>
            </SettingsRow>
          );
        })}
      </SettingsGroup>

      <SettingsGroup
        title="Add a server"
        description="Ollama, LM Studio, omlx, vLLM, or llama.cpp on this machine or your LAN, or a remote OpenAI-compatible service such as RunPod. A public remote needs HTTPS and your trust confirmation."
        testId="local-models-remote-copy"
      >
        <SettingsRow
          label="Server"
          description="Probe reads the model list. Save stores the server with one model."
          control={(
            <>
              <button
                type="button"
                className={SETTINGS_QUIET_BUTTON}
                disabled={off}
                onClick={() => void run({
                  type: "probe",
                  url,
                  api_key: apiKey,
                  accept_lan: acceptLan,
                  accept_remote: acceptRemote,
                }, "probe")}
              >
                Probe
              </button>
              <button
                type="button"
                className={SETTINGS_PRIMARY_BUTTON}
                disabled={off || !canSave}
                onClick={() => {
                  const parsedContext = Number.parseInt(contextTokens.trim(), 10);
                  const command: LocalModelCommand = {
                    type: "save_external",
                    url,
                    api_key: apiKey,
                    accept_lan: acceptLan,
                    accept_remote: acceptRemote,
                    model: attachModel,
                    name: displayName,
                  };
                  if (Number.isFinite(parsedContext) && parsedContext > 0) {
                    command.context_length = parsedContext;
                  }
                  void run(command, "save");
                }}
              >
                Save
              </button>
            </>
          )}
        >
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <label className={LABEL}>
                Endpoint URL
                <input value={url} onChange={(event) => setUrl(event.target.value)} className={FIELD} />
              </label>
              <label className={LABEL}>
                Display name
                <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} className={FIELD} />
              </label>
              <label className={LABEL}>
                Model id
                <input
                  value={manualModel}
                  onChange={(event) => setManualModel(event.target.value)}
                  placeholder="Required when the server lists no models"
                  className={FIELD}
                />
              </label>
              <label className={LABEL}>
                Context tokens
                <input
                  type="number"
                  min={1}
                  value={contextTokens}
                  onChange={(event) => setContextTokens(event.target.value)}
                  placeholder="262144"
                  className={FIELD}
                />
              </label>
              <label className={`${LABEL} col-span-2`}>
                Optional API key
                <input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} className={FIELD} />
              </label>
            </div>
            <label className="flex items-center gap-2 text-ui-11 text-txt">
              <input type="checkbox" checked={acceptLan} onChange={(event) => setAcceptLan(event.target.checked)} />
              This is a LAN machine I trust
            </label>
            <label className="flex items-center gap-2 text-ui-11 text-txt" data-testid="local-models-accept-remote">
              <input type="checkbox" checked={acceptRemote} onChange={(event) => setAcceptRemote(event.target.checked)} />
              I trust this public HTTPS remote service (RunPod or similar)
            </label>
            {probe ? (
              <div className="text-ui-11 text-muted space-y-1" data-testid="local-models-probe">
                <p>Detected {probe.vendor}. {probe.models.length} model{probe.models.length === 1 ? "" : "s"}.</p>
                {typeof probe.context_length === "number" && probe.context_length > 0 ? (
                  <p data-testid="local-models-probe-context">Context {probe.context_length.toLocaleString()} tokens</p>
                ) : null}
                {probe.models.length > 0 ? (
                  <label className={LABEL}>
                    Discovered model
                    <select
                      className={FIELD}
                      value={selectedDiscovered}
                      onChange={(event) => setSelectedDiscovered(event.target.value)}
                    >
                      {probe.models.map((model) => (
                        <option key={model} value={model}>{model}</option>
                      ))}
                    </select>
                  </label>
                ) : (
                  <p>The server lists no models. Enter a model id to save it.</p>
                )}
              </div>
            ) : null}
          </div>
        </SettingsRow>
      </SettingsGroup>
      {busy ? <p className="sr-only" aria-live="polite">{busy}</p> : null}
    </SettingsPage>
  );
}
