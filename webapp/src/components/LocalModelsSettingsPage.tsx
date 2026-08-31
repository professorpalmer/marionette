import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  Pause,
  Play,
  RefreshCw,
  Square,
  Trash2,
} from "lucide-react";
import {
  api,
  parseLocalToolCalling,
  type LocalCatalogModel,
  type LocalExternalEndpoint,
  type LocalModelCommand,
  type LocalModelProbeResult,
  type LocalModelStreamFrame,
  type LocalModelsSnapshot,
  type LocalToolCallingStatus,
} from "../lib/api";

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

function statusLabel(status: string, healthy?: boolean): string {
  if (status === "ready" && healthy) return "Ready";
  if (status === "ready") return "Installed";
  if (status === "downloading") return "Downloading";
  if (status === "extracting") return "Extracting";
  if (status === "error") return "Error";
  if (status === "paused") return "Paused";
  if (status === "absent") return "Not installed";
  return status || "Unknown";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isLocalModelsSnapshot(value: unknown): value is LocalModelsSnapshot {
  return isRecord(value) && isRecord(value.managed) && isRecord(value.hardware);
}

function isLocalModelStreamFrame(value: unknown): value is LocalModelStreamFrame {
  return isRecord(value) && typeof value.kind === "string";
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
  const [acceptLan, setAcceptLan] = useState(false);
  const [acceptRemote, setAcceptRemote] = useState(false);
  const [probe, setProbe] = useState<LocalModelProbeResult | null>(null);
  const [selectedDiscovered, setSelectedDiscovered] = useState("");
  const [selectedCatalogId, setSelectedCatalogId] = useState("");
  const liveSnapshot = useRef(false);
  const eventCursorRef = useRef(0);
  const pollStarted = useRef(false);
  const loadErrorRef = useRef(false);

  const acceptSnapshot = (
    next: LocalModelsSnapshot,
    source: "get" | "live" | "command",
  ): boolean => {
    if (!shouldAcceptSnapshot(next, eventCursorRef.current, {
      source,
      live: liveSnapshot.current,
    })) {
      return false;
    }
    eventCursorRef.current = Math.max(eventCursorRef.current, snapshotCursor(next));
    if (source !== "get") liveSnapshot.current = true;
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
          const models = catalogModels(next);
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
          since = Math.max(since, res.cursor || 0, snapshotCursor(res.snapshot));
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
        if (cancelled || !isLocalModelStreamFrame(ev)) return;
        if (ev.kind === "snapshot" && ev.snapshot && isLocalModelsSnapshot(ev.snapshot)) {
          acceptSnapshot(ev.snapshot, "live");
          since = Math.max(since, ev.cursor ?? 0, snapshotCursor(ev.snapshot));
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
    setBusy(label);
    setError("");
    try {
      const result = await api.localModelCommand(command);
      if (isProbeResult(result)) {
        setProbe(result);
        setSelectedDiscovered(result.models[0] || "");
      } else if (isLocalModelsSnapshot(result)) {
        acceptSnapshot(result, "command");
        if (command.type === "save_external") setApiKey("");
        if (command.type === "activate" || command.type === "start") {
          window.dispatchEvent(new Event("harness-config-changed"));
        }
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Local model command failed";
      setError(message);
    } finally {
      setBusy(null);
    }
  };

  if (loading && !snapshot) {
    return (
      <div className="max-w-2xl" data-testid="local-models-page">
        <h2 className="text-[15px] font-semibold text-txt mb-2">Local Models</h2>
        <p className="text-[12px] text-faint">Loading hardware and install state...</p>
      </div>
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
  const paused = runtimeStatus === "paused" || modelStatus === "paused";
  const installed = runtimeStatus === "ready" && modelStatus === "ready";
  const running = Boolean(process?.pid && process.healthy);
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

  return (
    <div className="max-w-2xl" data-testid="local-models-page">
      <h2 className="text-[15px] font-semibold text-txt mb-1">Local Models</h2>
      <p className="text-[12px] text-muted mb-4">
        Choose a catalog model to install and run, or attach an existing OpenAI-compatible
        service. Public remotes such as RunPod are supported over HTTPS after you confirm you
        trust them.
      </p>

      {alertText ? (
        <div
          role="alert"
          className="mb-3 flex items-start gap-2 rounded-md border border-edge/50 bg-panel2 px-3 py-2 text-[12px] text-txt"
          data-testid="local-models-error"
        >
          <AlertCircle size={14} className="mt-0.5 text-faint shrink-0" aria-hidden="true" />
          <span>{alertText}</span>
        </div>
      ) : null}

      <section className="mb-6" data-testid="local-models-hardware">
        <h3 className="text-[13px] font-semibold text-txt mb-1">This machine</h3>
        <p className="text-[12px] text-muted">
          {hardware?.platform_key || "unknown"} · {hardware?.accelerator || "cpu"}
          {hardware?.ram_bytes ? ` · ${formatBytes(hardware.ram_bytes)} RAM` : ""}
          {hardware?.disk_free_bytes ? ` · ${formatBytes(hardware.disk_free_bytes)} free` : ""}
        </p>
        {hardware?.supported ? (
          <p className="text-[12px] text-txt mt-1 flex items-center gap-1.5">
            <CheckCircle2 size={13} className="text-faint" aria-hidden="true" />
            <span>A pinned llama.cpp runtime is available for this platform.</span>
          </p>
        ) : (
          <p className="text-[12px] text-txt mt-1 flex items-center gap-1.5" data-testid="local-models-unsupported">
            <AlertCircle size={13} className="text-faint" aria-hidden="true" />
            <span>{hardware?.unsupported_reason || "Managed install is not available on this machine."}</span>
          </p>
        )}
      </section>

      <section className="mb-6" data-testid="local-models-managed">
        <h3 className="text-[13px] font-semibold text-txt mb-2">Managed llama.cpp</h3>
        <p className="text-[12px] text-muted mb-2">
          Pick which catalog model to download and run on this machine.
        </p>
        {models.length > 0 ? (
          <label className="block mb-3" data-testid="local-models-catalog-select">
            <span className="text-[11px] text-muted">Available model</span>
            <select
              className="mt-1 w-full px-2 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt"
              value={selectedModel?.id || ""}
              onChange={(event) => setSelectedCatalogId(event.target.value)}
            >
              {models.map((item) => (
                <option key={item.id} value={item.id}>{item.name || item.id}</option>
              ))}
            </select>
          </label>
        ) : (
          <p className="text-[12px] text-faint mb-3">No catalog models are packaged in this build.</p>
        )}
        {selectedModel ? (
          <dl className="mb-3 text-[12px] text-muted" data-testid="local-models-catalog-facts">
            <div>{selectedModel.name}</div>
            <div>Source: {selectedModel.source || "catalog"} · Trust: {selectedModel.trust || "catalog"}</div>
            <div>
              Size {formatBytes(selectedModel.size)}
              {selectedModel.min_ram_gb ? ` · RAM ${selectedModel.min_ram_gb} GB` : ""}
              {selectedModel.min_disk_bytes ? ` · Disk ${formatBytes(selectedModel.min_disk_bytes)}` : ""}
              {selectedModel.context_length ? ` · Context ${selectedModel.context_length}` : ""}
            </div>
          </dl>
        ) : null}
        <p className="text-[12px] text-muted mb-2" data-testid="local-models-managed-status">
          Runtime {statusLabel(runtimeStatus)} · Model {statusLabel(modelStatus)}
          {running ? " · Server running" : installed ? " · Server stopped" : ""}
          {process?.context_length ? ` · ${process.context_length} context` : ""}
        </p>
        {download && (installing || paused) ? (
          <div className="mb-3" data-testid="local-models-progress">
            <div className="h-1.5 rounded bg-panel2 overflow-hidden">
              <div
                className="h-full bg-accent motion-reduce:transition-none"
                style={{ width: `${progress}%` }}
              />
            </div>
            <p className="text-[11px] text-muted mt-1">
              {download.phase || "download"} · {formatBytes(download.bytes)} / {formatBytes(download.total)}
            </p>
          </div>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {!installed && !installing && !paused ? (
            <button
              type="button"
              className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              disabled={busy !== null || !hardware?.supported || !selectedModel?.id}
              onClick={() => void run({
                type: "install",
                target: "all",
                model_id: selectedModel?.id || "",
              }, "install")}
            >
              <span className="inline-flex items-center gap-1.5">
                <Download size={13} aria-hidden="true" />
                Install
              </span>
            </button>
          ) : null}
          {paused ? (
            <button
              type="button"
              className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              disabled={busy !== null || !hardware?.supported || !selectedModel?.id}
              onClick={() => void run({
                type: "install",
                target: "all",
                model_id: selectedModel?.id || "",
              }, "resume")}
            >
              <span className="inline-flex items-center gap-1.5">
                <Download size={13} aria-hidden="true" />
                Resume
              </span>
            </button>
          ) : null}
          {runtimeStatus === "downloading" || modelStatus === "downloading" ? (
            <button
              type="button"
              className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              disabled={busy !== null}
              onClick={() => void run({ type: "cancel", target: "all" }, "cancel")}
            >
              <span className="inline-flex items-center gap-1.5">
                <Pause size={13} aria-hidden="true" />
                Cancel
              </span>
            </button>
          ) : null}
          {installed && !running ? (
            <button
              type="button"
              className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              disabled={busy !== null}
              onClick={() => void run({ type: "start" }, "start")}
            >
              <span className="inline-flex items-center gap-1.5">
                <Play size={13} aria-hidden="true" />
                Start
              </span>
            </button>
          ) : null}
          {running ? (
            <>
              <button
                type="button"
                className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                disabled={busy !== null}
                onClick={() => void run({ type: "stop" }, "stop")}
              >
                <span className="inline-flex items-center gap-1.5">
                  <Square size={13} aria-hidden="true" />
                  Stop
                </span>
              </button>
              <button
                type="button"
                className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                disabled={busy !== null}
                onClick={() => void run({ type: "restart" }, "restart")}
              >
                <span className="inline-flex items-center gap-1.5">
                  <RefreshCw size={13} aria-hidden="true" />
                  Restart
                </span>
              </button>
              <button
                type="button"
                className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                disabled={busy !== null || !spec}
                onClick={() => void run({ type: "activate", spec }, "activate")}
              >
                {active === spec ? "Active in picker" : "Activate"}
              </button>
            </>
          ) : null}
          {modelPresent ? (
            <button
              type="button"
              className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              disabled={busy !== null}
              onClick={() => void run({ type: "remove", target: "model" }, "remove")}
            >
              <span className="inline-flex items-center gap-1.5">
                <Trash2 size={13} aria-hidden="true" />
                Remove model
              </span>
            </button>
          ) : null}
          {runtimePresent ? (
            <button
              type="button"
              className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              disabled={busy !== null}
              onClick={() => void run({ type: "remove", target: "runtime" }, "remove")}
            >
              <span className="inline-flex items-center gap-1.5">
                <Trash2 size={13} aria-hidden="true" />
                Remove runtime
              </span>
            </button>
          ) : null}
          {modelPresent && runtimePresent ? (
            <button
              type="button"
              className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
              disabled={busy !== null}
              onClick={() => void run({ type: "remove", target: "all" }, "remove")}
            >
              <span className="inline-flex items-center gap-1.5">
                <Trash2 size={13} aria-hidden="true" />
                Remove all
              </span>
            </button>
          ) : null}
        </div>
      </section>

      <section data-testid="local-models-external">
        <h3 className="text-[13px] font-semibold text-txt mb-2">Existing or remote service</h3>
        <p className="text-[12px] text-muted mb-2" data-testid="local-models-remote-copy">
          RunPod or any remote OpenAI-compatible service, plus Ollama, LM Studio, omlx, vLLM,
          and llama.cpp on this machine or LAN. HTTPS and an explicit trust confirmation are
          required for public remotes.
        </p>
        <p className="text-[12px] text-muted mb-2" data-testid="local-models-tool-calling-copy">
          The check records whether the endpoint returned the requested tool call. It does
          not execute tools or enroll the model as a Puppetmaster swarm worker.
        </p>
        <label className="block text-[11px] text-muted mb-1" htmlFor="local-endpoint-url">
          Endpoint URL
        </label>
        <input
          id="local-endpoint-url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          className="w-full mb-2 px-2.5 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        />
        <label className="block text-[11px] text-muted mb-1" htmlFor="local-endpoint-name">
          Display name
        </label>
        <input
          id="local-endpoint-name"
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          className="w-full mb-2 px-2.5 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        />
        <label className="block text-[11px] text-muted mb-1" htmlFor="local-endpoint-model">
          Model id
        </label>
        <input
          id="local-endpoint-model"
          value={manualModel}
          onChange={(event) => setManualModel(event.target.value)}
          placeholder="Required if the server does not list models"
          className="w-full mb-2 px-2.5 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        />
        <label className="block text-[11px] text-muted mb-1" htmlFor="local-endpoint-key">
          Optional API key
        </label>
        <input
          id="local-endpoint-key"
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          className="w-full mb-2 px-2.5 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        />
        <label className="flex items-center gap-2 text-[12px] text-txt mb-2">
          <input
            type="checkbox"
            checked={acceptLan}
            onChange={(event) => setAcceptLan(event.target.checked)}
          />
          This is a LAN machine I trust
        </label>
        <label className="flex items-center gap-2 text-[12px] text-txt mb-3" data-testid="local-models-accept-remote">
          <input
            type="checkbox"
            checked={acceptRemote}
            onChange={(event) => setAcceptRemote(event.target.checked)}
          />
          I trust this public HTTPS remote service (RunPod or similar)
        </label>
        <div className="flex flex-wrap gap-2 mb-3">
          <button
            type="button"
            className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            disabled={busy !== null}
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
            className="px-2.5 py-1.5 rounded-md border border-edge/40 text-[12px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            disabled={busy !== null || !canSave}
            onClick={() => void run({
              type: "save_external",
              url,
              api_key: apiKey,
              accept_lan: acceptLan,
              accept_remote: acceptRemote,
              model: attachModel,
              name: displayName,
            }, "save")}
          >
            Save
          </button>
        </div>
        {probe ? (
          <div className="mb-3 text-[12px] text-muted" data-testid="local-models-probe">
            <p>Detected {probe.vendor}. {probe.models.length} model{probe.models.length === 1 ? "" : "s"}.</p>
            {probe.models.length > 0 ? (
              <label className="block mt-2">
                <span className="text-[11px] text-muted">Discovered model</span>
                <select
                  className="mt-1 w-full px-2 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt"
                  value={selectedDiscovered}
                  onChange={(event) => setSelectedDiscovered(event.target.value)}
                >
                  {probe.models.map((model) => (
                    <option key={model} value={model}>{model}</option>
                  ))}
                </select>
              </label>
            ) : (
              <p className="mt-2">No models listed. Enter a model id above to save this endpoint.</p>
            )}
          </div>
        ) : null}

        {(snapshot?.externals || []).length === 0 ? (
          <p className="text-[12px] text-faint" data-testid="local-models-empty-external">
            No saved servers yet. Probe a URL or save one with a model id.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {(snapshot?.externals || []).map((endpoint: LocalExternalEndpoint) => {
              const endpointSpec = `local:${endpoint.id}/${endpoint.selected_model || ""}`;
              const toolCalling = parseLocalToolCalling(endpoint.tool_calling);
              return (
                <li
                  key={endpoint.id}
                  className="rounded-md border border-edge/40 px-3 py-2"
                  data-testid={`local-external-${endpoint.id}`}
                >
                  <p className="text-[12px] text-txt">
                    {endpoint.name || endpoint.id} · {endpoint.vendor}
                    {endpoint.healthy ? " · reachable" : " · saved"}
                  </p>
                  <p className="text-[11px] text-muted truncate">{endpoint.base_url}</p>
                  <p
                    className="text-[11px] text-muted mt-1"
                    data-testid={`local-external-tool-calling-${endpoint.id}`}
                  >
                    Tool calling {TOOL_CALLING_LABELS[toolCalling.status]}
                    {toolCalling.reason ? ` · ${toolCalling.reason}` : ""}
                  </p>
                  <div className="flex flex-wrap gap-2 mt-2">
                    <button
                      type="button"
                      className="px-2 py-1 rounded-md border border-edge/40 text-[11px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                      disabled={busy !== null}
                      onClick={() => void run({ type: "activate", spec: endpointSpec }, "activate")}
                    >
                      {active === endpointSpec ? "Active in picker" : "Activate"}
                    </button>
                    <button
                      type="button"
                      className="px-2 py-1 rounded-md border border-edge/40 text-[11px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                      disabled={busy !== null}
                      onClick={() => void run({ type: "verify_tool_calling", spec: endpointSpec }, "verify")}
                    >
                      Test tool calling
                    </button>
                    <button
                      type="button"
                      className="px-2 py-1 rounded-md border border-edge/40 text-[11px] text-txt hover:bg-panel2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                      disabled={busy !== null}
                      onClick={() => void run({ type: "remove", target: "all", endpoint_id: endpoint.id }, "remove")}
                    >
                      Delete
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
      {busy ? (
        <p className="sr-only" aria-live="polite">{busy}</p>
      ) : null}
    </div>
  );
}
