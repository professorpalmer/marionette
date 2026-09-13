import type { LocalManagedState, LocalModelsSnapshot } from "./api";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function integer(value: unknown): value is number {
  return finite(value) && Number.isSafeInteger(value);
}

function nullableNumber(value: unknown): boolean {
  return value === null || finite(value);
}

function textFields(value: Record<string, unknown>, keys: string[]): boolean {
  return keys.every((key) => value[key] === undefined || typeof value[key] === "string");
}

function numberFields(value: Record<string, unknown>, keys: string[]): boolean {
  return keys.every((key) => value[key] === undefined || finite(value[key]));
}

function nullableNumberFields(value: Record<string, unknown>, keys: string[]): boolean {
  return keys.every((key) => value[key] === undefined || nullableNumber(value[key]));
}

function boolFields(value: Record<string, unknown>, keys: string[]): boolean {
  return keys.every((key) => value[key] === undefined || typeof value[key] === "boolean");
}

function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function component(value: unknown): boolean {
  return isRecord(value) && typeof value.status === "string"
    && textFields(value, ["path", "id", "platform", "release"])
    && (value.error === undefined || value.error === null || typeof value.error === "string");
}

function isManaged(value: unknown): value is LocalManagedState {
  if (!isRecord(value) || !component(value.runtime) || !component(value.model)
    || !integer(value.idle_timeout_minutes) || value.idle_timeout_minutes > 1440
    || value.idle_timeout_enabled !== (value.idle_timeout_minutes > 0)
    || !integer(value.active_requests) || !finite(value.observed_at)
    || !nullableNumber(value.last_activity_at) || !nullableNumber(value.idle_deadline_at)
    || !nullableNumber(value.idle_remaining_seconds)
    || !(value.lifecycle_error === null || typeof value.lifecycle_error === "string")
    || !textFields(value, ["spec"]) || !boolFields(value, ["usable"])) return false;
  const process = value.process;
  if (process !== null && process !== undefined && (
    !isRecord(process) || !nullableNumberFields(process, ["pid", "port", "context_length"])
    || !textFields(process, ["host"]) || !boolFields(process, ["healthy"])
  )) return false;
  if (value.downloads !== undefined && (!isRecord(value.downloads)
    || !Object.values(value.downloads).every((row) => isRecord(row)
      && textFields(row, ["filename", "phase"]) && numberFields(row, ["bytes", "total"])))) return false;
  switch (value.residency) {
    case "stopped":
      return !process && (value.stop_reason === null || value.stop_reason === "inactivity");
    case "running":
      return isRecord(process) && integer(process.pid) && process.pid > 0
        && process.healthy === true && value.stop_reason === null;
    case "starting": case "stopping": case "unknown": case "error":
      return value.stop_reason === null;
    default:
      return false;
  }
}

function catalogModel(value: unknown): boolean {
  return isRecord(value) && typeof value.id === "string" && typeof value.name === "string"
    && textFields(value, ["quant", "source", "trust"])
    && numberFields(value, ["size", "context_length", "min_ram_gb", "min_disk_bytes"]);
}

function external(value: unknown): boolean {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.vendor !== "string"
    || typeof value.base_url !== "string" || !strings(value.models)
    || !textFields(value, ["name", "selected_model", "kind"])
    || !nullableNumberFields(value, ["context_length"])
    || !boolFields(value, ["has_key", "lan_accepted", "remote_accepted", "requires_key", "healthy"])
    || !(value.last_error === undefined || value.last_error === null || typeof value.last_error === "string")) return false;
  const tools = value.tool_calling;
  return tools === undefined || (isRecord(tools)
    && typeof tools.status === "string" && textFields(tools, ["reason"])
    && nullableNumberFields(tools, ["checked_at"]));
}

export function isLocalModelsSnapshot(value: unknown): value is LocalModelsSnapshot {
  if (!isRecord(value) || !isManaged(value.managed) || !isRecord(value.hardware)
    || !isRecord(value.catalog) || !Array.isArray(value.externals) || !value.externals.every(external)
    || !textFields(value, ["active_spec", "error", "code"])
    || (value.event_cursor !== undefined && !integer(value.event_cursor))
    || (value.usable_specs !== undefined && !strings(value.usable_specs))) return false;
  const hardware = value.hardware;
  if (!["os", "arch", "platform_key", "accelerator"].every((key) => typeof hardware[key] === "string")
    || typeof hardware.supported !== "boolean"
    || !textFields(hardware, ["unsupported_reason"])
    || !nullableNumberFields(hardware, ["ram_bytes", "disk_free_bytes"])
    || !numberFields(hardware, ["min_ram_gb"])
    || !boolFields(hardware, ["runtime_available"])) return false;
  const catalog = value.catalog;
  if (!textFields(catalog, ["runtime_release"])
    || (catalog.models !== undefined && (!Array.isArray(catalog.models) || !catalog.models.every(catalogModel)))) return false;
  if (catalog.model !== undefined && catalog.model !== null && (!isRecord(catalog.model)
    || !textFields(catalog.model, ["id", "name", "quant"])
    || !numberFields(catalog.model, ["size", "context_length"]))) return false;
  if (catalog.runtime !== undefined && catalog.runtime !== null && (!isRecord(catalog.runtime)
    || !textFields(catalog.runtime, ["filename", "platform", "backend"])
    || !numberFields(catalog.runtime, ["size"]))) return false;
  return value.events === undefined || (Array.isArray(value.events) && value.events.every((event) =>
    isRecord(event) && integer(event.cursor) && typeof event.kind === "string"
    && (event.data === undefined || isRecord(event.data)) && numberFields(event, ["ts"])));
}

export function residencyLabel(managed: LocalManagedState): string {
  const residency = managed.residency;
  switch (residency) {
    case "running": return "Running";
    case "starting": return "Starting";
    case "stopping": return "Stopping";
    case "stopped": return managed.stop_reason === "inactivity" ? "Stopped after inactivity" : "Stopped";
    case "unknown": case "error": return "Status unknown";
    default: {
      const exhaustive: never = residency;
      return exhaustive;
    }
  }
}

export function parseIdleMinutes(text: string): number | null {
  if (!/^\d+$/.test(text)) return null;
  const minutes = Number(text);
  return Number.isSafeInteger(minutes) && minutes <= 1440 ? minutes : null;
}
