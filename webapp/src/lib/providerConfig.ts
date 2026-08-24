import type { ProviderInfo } from "./api";

export const PROVIDER_CONFIG_FIELD_IDS = [
  "name",
  "display_name",
  "api_key",
  "base_url",
  "api_mode",
] as const;

export type ProviderConfigFieldId = (typeof PROVIDER_CONFIG_FIELD_IDS)[number];

export type ProviderConfigValues = Record<ProviderConfigFieldId, string>;

export type ProviderConfigGroup = {
  id: string;
  label: string;
  fields: ProviderConfigFieldId[];
};

export const PROVIDER_CONFIG_GROUPS: ProviderConfigGroup[] = [
  { id: "identity", label: "Identity", fields: ["name", "display_name"] },
  { id: "credentials", label: "Credentials", fields: ["api_key"] },
  { id: "connection", label: "Connection", fields: ["base_url", "api_mode"] },
];

export const SECRET_FIELD_IDS = new Set<ProviderConfigFieldId>(["api_key"]);

export const PROVIDER_CONFIG_LABELS: Record<ProviderConfigFieldId, string> = {
  name: "Provider",
  display_name: "Display name",
  api_key: "API key",
  base_url: "Base URL",
  api_mode: "API mode",
};

export function emptyProviderConfig(): ProviderConfigValues {
  return {
    name: "",
    display_name: "",
    api_key: "",
    base_url: "",
    api_mode: "",
  };
}

/** Never prefill secrets — the user must retype to replace. */
export function providerConfigFromInfo(
  provider?: Partial<ProviderInfo> | null,
): ProviderConfigValues {
  return {
    name: provider?.name || "",
    display_name: provider?.display_name || "",
    api_key: "",
    base_url: provider?.base_url || "",
    api_mode: provider?.api_mode || "",
  };
}

export function isSecretRetyped(draftSecret: string, masked?: string): boolean {
  const typed = (draftSecret || "").trim();
  if (!typed) return false;
  if (masked && typed === masked.trim()) return false;
  return true;
}

/** Submit payload: only fields the user changed. Secrets only if retyped. */
export function changedProviderFields(
  original: ProviderConfigValues,
  draft: ProviderConfigValues,
  opts?: { maskedSecret?: string },
): Partial<ProviderConfigValues> {
  const out: Partial<ProviderConfigValues> = {};
  for (const id of PROVIDER_CONFIG_FIELD_IDS) {
    const next = (draft[id] ?? "").trim();
    const prev = (original[id] ?? "").trim();
    if (SECRET_FIELD_IDS.has(id)) {
      if (isSecretRetyped(next, opts?.maskedSecret)) out[id] = next;
      continue;
    }
    if (next !== prev) out[id] = next;
  }
  return out;
}
