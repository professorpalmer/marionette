import { useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import type { ProviderInfo } from "../lib/api";
import { OverlayPortal } from "../lib/overlayPortal";
import {
  PROVIDER_CONFIG_GROUPS,
  PROVIDER_CONFIG_LABELS,
  SECRET_FIELD_IDS,
  changedProviderFields,
  emptyProviderConfig,
  providerConfigFromInfo,
  type ProviderConfigFieldId,
  type ProviderConfigValues,
} from "../lib/providerConfig";

export type ProviderConfigModalProps = {
  open: boolean;
  /** Add-provider path: name is editable and fields start empty. */
  manual?: boolean;
  provider?: Partial<ProviderInfo> | null;
  busy?: boolean;
  onClose: () => void;
  onSubmit: (changed: Partial<ProviderConfigValues>) => void | Promise<void>;
};

export default function ProviderConfigModal({
  open,
  manual = false,
  provider = null,
  busy = false,
  onClose,
  onSubmit,
}: ProviderConfigModalProps) {
  const original = useMemo(
    () => (manual ? emptyProviderConfig() : providerConfigFromInfo(provider)),
    [manual, provider],
  );
  const [draft, setDraft] = useState<ProviderConfigValues>(original);
  const firstFieldRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) setDraft(original);
  }, [open, original]);

  const changed = useMemo(
    () => changedProviderFields(original, draft, { maskedSecret: provider?.masked }),
    [original, draft, provider?.masked],
  );
  const canSubmit = Object.keys(changed).length > 0 && !busy;
  const title = manual
    ? "Add provider"
    : `Configure ${provider?.display_name || provider?.name || "provider"}`;

  const setField = (id: ProviderConfigFieldId, value: string) => {
    setDraft((prev) => ({ ...prev, [id]: value }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    await onSubmit(changed);
  };

  return (
    <OverlayPortal
      open={open}
      onClose={onClose}
      testId="provider-config-modal"
      className="fixed inset-0 z-[90] bg-black/50 flex items-center justify-center p-4"
      initialFocusRef={firstFieldRef}
      onBackdropClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <form
        role="dialog"
        aria-modal="true"
        aria-label={title}
        data-manual={manual ? "true" : "false"}
        onSubmit={handleSubmit}
        className="w-full max-w-md rounded-lg border border-edge bg-bg shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-edge/40">
          <h2 className="text-[13px] font-semibold text-txt">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            title="Close"
            className="p-1 rounded-md text-muted hover:text-txt hover:bg-panel2"
          >
            <X size={14} />
          </button>
        </div>

        <div className="px-4 py-3 space-y-4 max-h-[70vh] overflow-y-auto">
          {PROVIDER_CONFIG_GROUPS.map((group) => (
            <fieldset
              key={group.id}
              data-testid={`provider-config-group-${group.id}`}
              className="space-y-2"
            >
              <legend className="text-[10px] uppercase tracking-wide text-muted font-medium">
                {group.label}
              </legend>
              {group.fields.map((id) => {
                const secret = SECRET_FIELD_IDS.has(id);
                const locked = id === "name" && !manual;
                const placeholder = secret
                  ? provider?.has_key
                    ? provider.masked || "Retype to replace"
                    : PROVIDER_CONFIG_LABELS[id]
                  : PROVIDER_CONFIG_LABELS[id];
                return (
                  <label key={id} className="block space-y-1">
                    <span className="text-[11px] text-muted">{PROVIDER_CONFIG_LABELS[id]}</span>
                    <input
                      ref={id === "name" ? firstFieldRef : undefined}
                      data-testid={`provider-config-field-${id}`}
                      type={secret ? "password" : "text"}
                      name={id}
                      autoComplete={secret ? "off" : "off"}
                      value={draft[id]}
                      onChange={(e) => setField(id, e.target.value)}
                      disabled={busy || locked}
                      placeholder={placeholder}
                      readOnly={locked}
                      className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt text-[11px] font-mono focus:outline-none focus:border-accent disabled:opacity-50"
                    />
                    {secret && provider?.has_key ? (
                      <span className="block text-[10px] text-faint">
                        Leave blank to keep the current key. Retype to replace.
                      </span>
                    ) : null}
                  </label>
                );
              })}
            </fieldset>
          ))}
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-edge/40">
          <button
            type="button"
            onClick={onClose}
            className="text-muted hover:text-txt border border-edge rounded px-2.5 py-1 text-[11px]"
          >
            Cancel
          </button>
          <button
            type="submit"
            data-testid="provider-config-submit"
            disabled={!canSubmit}
            className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 rounded px-2.5 py-1 text-[11px] font-medium disabled:opacity-30"
          >
            Save
          </button>
        </div>
      </form>
    </OverlayPortal>
  );
}
