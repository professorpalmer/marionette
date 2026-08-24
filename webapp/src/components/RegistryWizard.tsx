import { useEffect, useMemo, useState } from "react";
import { ExternalLink, KeyRound } from "lucide-react";
import { api, type ProviderInfo } from "../lib/api";
import {
  defaultOnboardingProvider,
  onboardableProviders,
  onboardingCopy,
  openOnboardingKeyUrl,
} from "../lib/onboardingProviders";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import { setConfigured, skipFirstRun } from "../state/onboardingStore";

interface RegistryWizardProps {
  onClose: () => void;
}

function dismissWizard(configured = false): void {
  if (configured) {
    setConfigured(true);
  } else {
    skipFirstRun();
  }
}

export default function RegistryWizard({ onClose }: RegistryWizardProps) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [selected, setSelected] = useState("");
  const [keyValue, setKeyValue] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const errorNotice = usePanelNotice(error || null);

  const tiles = useMemo(() => onboardableProviders(providers), [providers]);
  const copy = onboardingCopy(selected);
  const selectedProvider = tiles.find((p) => p.name === selected);
  const canConnect = Boolean(keyValue.trim()) && !saving && Boolean(selected);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        dismissWizard();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError("");
      try {
        const list = await api.providers();
        if (cancelled) return;
        setProviders(list);
        setSelected(defaultOnboardingProvider(list));
      } catch (err: unknown) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : "Failed to load providers.";
        setError(message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const skip = () => {
    dismissWizard();
    onClose();
  };

  const connect = async () => {
    const key = keyValue.trim();
    if (!selected || !key) return;
    setSaving(true);
    setError("");
    try {
      const res = await api.setProviderKey(selected, key);
      if (!res?.ok) {
        throw new Error("Could not save that key.");
      }
      window.dispatchEvent(new Event("harness-config-changed"));
      dismissWizard(true);
      onClose();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Could not save that key.";
      setError(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      data-testid="provider-onboarding"
      className="fixed inset-0 z-50 flex items-center justify-center px-4 py-6"
      style={{
        background:
          "radial-gradient(ellipse at 50% 38%, rgba(224,164,90,0.08) 0%, transparent 52%), #0f1113",
      }}
    >
      <div className="onboarding-card w-full max-w-[34rem] max-h-full overflow-y-auto rounded-2xl border border-edge bg-panel px-7 py-8 shadow-[0_24px_80px_rgba(0,0,0,0.45)]">
        <header className="text-center mb-6">
          <h1 className="text-[1.45rem] font-semibold tracking-tight text-txt leading-tight">
            Let&apos;s get you set up with Marionette
          </h1>
          <p className="mt-2 text-[13px] text-muted leading-relaxed">
            Connect a model provider to start chatting. One key runs chat and swarms.
          </p>
        </header>

        {loading ? (
          <p className="text-center text-muted text-[13px] py-10">Loading providers…</p>
        ) : tiles.length === 0 ? (
          <p className="text-center text-muted text-[13px] py-8">
            No Full stack providers available. You can add a key later from Settings.
          </p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2" role="listbox" aria-label="Model providers">
              {tiles.map((p) => {
                const active = p.name === selected;
                const tileCopy = onboardingCopy(p.name);
                return (
                  <button
                    key={p.name}
                    type="button"
                    role="option"
                    aria-selected={active}
                    onClick={() => {
                      setSelected(p.name);
                      setKeyValue("");
                      setError("");
                    }}
                    className={`relative text-left rounded-lg border px-3 py-2.5 transition-colors ${
                      active
                        ? "border-accent bg-accent/10"
                        : "border-edge/70 bg-panel2 hover:border-edge2"
                    }`}
                  >
                    {active ? (
                      <span className="absolute inset-y-0 right-0 w-[3px] rounded-r-lg bg-accent" />
                    ) : null}
                    <div className="text-[13px] font-semibold text-txt leading-tight pr-2">
                      {p.display_name || p.name}
                    </div>
                    <div className="text-[11px] text-muted mt-0.5 leading-snug pr-2">
                      {tileCopy.tagline}
                    </div>
                  </button>
                );
              })}
            </div>

            <div className="mt-5 flex items-start justify-between gap-3">
              <p className="text-[12.5px] text-muted leading-relaxed min-h-[2.5rem]">
                {copy.blurb}
              </p>
              {copy.keyUrl ? (
                <button
                  type="button"
                  onClick={() => openOnboardingKeyUrl(copy.keyUrl!)}
                  className="shrink-0 inline-flex items-center gap-1 text-[12px] text-accent hover:underline pt-0.5"
                >
                  Get a key
                  <ExternalLink size={11} />
                </button>
              ) : null}
            </div>

            <form
              className="mt-3"
              onSubmit={(e) => {
                e.preventDefault();
                void connect();
              }}
            >
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                placeholder={selectedProvider?.env_var || "API key"}
                value={keyValue}
                onChange={(e) => setKeyValue(e.target.value)}
                disabled={saving || !selected}
                className="w-full bg-bg border border-edge rounded-lg px-3 py-2.5 text-[13px] font-mono text-txt placeholder:text-faint focus:outline-none focus:border-accent disabled:opacity-50"
              />
              {errorNotice ? (
                <p className="mt-2 text-[12px] text-risk" role="alert">
                  {errorNotice}
                </p>
              ) : null}
              <div className="mt-3 flex justify-end">
                <button
                  type="submit"
                  disabled={!canConnect}
                  className="inline-flex items-center gap-1.5 bg-accent text-panel font-semibold rounded-lg px-4 py-2 text-[13px] hover:brightness-110 disabled:opacity-35 disabled:hover:brightness-100 transition"
                >
                  <KeyRound size={14} />
                  {saving ? "Connecting…" : "Connect"}
                </button>
              </div>
            </form>
          </>
        )}

        <div className="mt-6 text-center">
          <button
            type="button"
            onClick={skip}
            className="text-[12.5px] text-muted hover:text-txt transition-colors"
          >
            I&apos;ll choose a provider later
          </button>
        </div>
      </div>
    </div>
  );
}
