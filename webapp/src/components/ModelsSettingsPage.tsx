import { useEffect, useMemo, useState } from "react";
import { Search } from "lucide-react";
import { api, type ModelCatalogEntry } from "../lib/api";
import { modelFactParts, parseModelFacts } from "../lib/pilotModelFacts";
import { SwitchTrack } from "./ui/Switch";
import { SETTINGS_QUIET_BUTTON, SettingsGroup, SettingsPage, SettingsRow } from "./SettingsLayout";

/** A provider with more models than this shows only its enabled models until "Show all". */
const SHORT_LIST = 12;
const CATALOG_SNAPSHOT_KEY = "pmharness.models.catalogSnapshot";
let memoryCatalogSnapshot: ModelCatalogEntry[] | null = null;
let didForceRefreshThisSession = false;

type ProviderGroup = { provider: string; display: string; items: ModelCatalogEntry[] };

export function orderModelsEnabledFirst(items: ModelCatalogEntry[]): ModelCatalogEntry[] {
  return [...items].sort((a, b) => Number(b.enabled) - Number(a.enabled)
    || (a.name || a.model).localeCompare(b.name || b.model)
    || a.model.localeCompare(b.model));
}

export function clearCatalogSnapshot() {
  memoryCatalogSnapshot = null;
  didForceRefreshThisSession = false;
  try {
    localStorage.removeItem(CATALOG_SNAPSHOT_KEY);
  } catch {
    /* storage can be unavailable */
  }
}

export function readCatalogSnapshot(): ModelCatalogEntry[] {
  if (memoryCatalogSnapshot) return memoryCatalogSnapshot;
  try {
    const raw = localStorage.getItem(CATALOG_SNAPSHOT_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (Array.isArray(parsed?.catalog)) {
      memoryCatalogSnapshot = parsed.catalog;
      return parsed.catalog;
    }
  } catch {
    /* a corrupt snapshot is replaced by the next load */
  }
  return [];
}

function writeCatalogSnapshot(catalog: ModelCatalogEntry[]) {
  memoryCatalogSnapshot = catalog;
  try {
    localStorage.setItem(CATALOG_SNAPSHOT_KEY, JSON.stringify({ catalog, savedAt: Date.now() }));
  } catch {
    /* storage can be full */
  }
}

function displayName(entry: ModelCatalogEntry): string {
  return entry.name && entry.name !== entry.model ? entry.name : entry.model;
}

/**
 * Settings > Models: one row per model with the facts the pilot picker shows.
 * The switch decides if the model is in the picker (`/api/models/toggle`).
 */
export default function ModelsSettingsPage() {
  const [catalog, setCatalog] = useState<ModelCatalogEntry[]>(readCatalogSnapshot);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(() => catalog.length === 0);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [showAll, setShowAll] = useState<Record<string, boolean>>({});

  const load = async (opts?: { refresh?: boolean }) => {
    const hadSnapshot = readCatalogSnapshot().length > 0;
    // A snapshot stays on screen while the live catalog loads.
    if (!hadSnapshot) setLoading(true);
    else if (opts?.refresh) setRefreshing(true);
    try {
      const res = await api.modelCatalog({ refresh: !!opts?.refresh });
      const next = res.catalog || [];
      setCatalog(next);
      writeCatalogSnapshot(next);
    } catch {
      // Keep the last snapshot: a slow provider must not empty the page.
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    const force = !didForceRefreshThisSession;
    didForceRefreshThisSession = true;
    void load({ refresh: force });
  }, []);

  const toggle = async (entry: ModelCatalogEntry) => {
    if (busy === entry.spec) return;
    const next = !entry.enabled;
    setBusy(entry.spec);
    const put = (enabled: boolean) => setCatalog((prev) => {
      const updated = prev.map((c) => (c.spec === entry.spec ? { ...c, enabled } : c));
      writeCatalogSnapshot(updated);
      return updated;
    });
    put(next);
    try {
      await api.toggleModel(entry.spec, next);
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch {
      put(!next);
    } finally {
      setBusy(null);
    }
  };

  const facts = useMemo(() => parseModelFacts({ all: catalog }), [catalog]);

  const q = query.trim().toLowerCase();
  const groups = useMemo(() => {
    const filtered = q
      ? catalog.filter((c) => (
        c.model.toLowerCase().includes(q)
        || (c.name || "").toLowerCase().includes(q)
        || c.provider_display.toLowerCase().includes(q)
      ))
      : catalog;
    const out: ProviderGroup[] = [];
    for (const c of filtered) {
      let g = out.find((x) => x.provider === c.provider);
      if (!g) {
        g = { provider: c.provider, display: c.provider_display, items: [] };
        out.push(g);
      }
      g.items.push(c);
    }
    return out.map((g) => ({ ...g, items: orderModelsEnabledFirst(g.items) }));
  }, [catalog, q]);

  const enabledCount = catalog.filter((c) => c.enabled).length;
  const description = enabledCount > 0
    ? `Choose the models the pilot picker shows. ${enabledCount} on.`
    : "Choose the models the pilot picker shows. None is on, so the picker shows every available model.";

  return (
    <SettingsPage title="Models" description={description} testId="models-page">
      <div className="flex items-center gap-2">
        <div className="flex flex-1 items-center gap-2 px-2.5 py-1.5 rounded-md bg-panel2 border border-edge/50">
          <Search size={13} className="text-faint shrink-0" aria-hidden="true" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search models or providers"
            aria-label="Search models or providers"
            className="bg-transparent text-ui-12 text-txt placeholder:text-faint outline-none w-full"
          />
        </div>
        <button
          type="button"
          onClick={() => { void load({ refresh: true }); }}
          disabled={refreshing}
          title="Read the live model lists from each provider again"
          className={SETTINGS_QUIET_BUTTON}
        >
          {loading || refreshing ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {loading && catalog.length === 0 ? (
        <p className="text-ui-12 text-faint py-6 text-center">Loading model catalog...</p>
      ) : groups.length === 0 ? (
        <p className="text-ui-12 text-faint py-6 text-center">
          {q ? `No model matches "${query.trim()}".` : "No models are available. Connect a provider in Accounts first."}
        </p>
      ) : (
        groups.map((g) => {
          const onCount = g.items.filter((i) => i.enabled).length;
          const short = !q && !showAll[g.provider] && g.items.length > SHORT_LIST;
          const visible = short ? g.items.filter((i) => i.enabled) : g.items;
          const hidden = g.items.length - visible.length;
          return (
            <SettingsGroup
              key={g.provider}
              title={g.display}
              description={`${g.items.length} model${g.items.length === 1 ? "" : "s"}${onCount > 0 ? `, ${onCount} on` : ""}`}
              testId={`models-group-${g.provider}`}
            >
              {visible.map((entry) => {
                const name = displayName(entry);
                const parts = facts.get(entry.spec);
                const factLine = parts ? modelFactParts(parts).join(" · ") : "";
                return (
                  <SettingsRow
                    key={entry.spec}
                    testId="model-row"
                    label={(
                      <span className="flex items-baseline gap-2 min-w-0">
                        <span className="truncate">{name}</span>
                        {name !== entry.model ? (
                          <span className="text-ui-10 text-faint font-mono truncate">{entry.model}</span>
                        ) : null}
                      </span>
                    )}
                    description={factLine || undefined}
                    control={(
                      <button
                        type="button"
                        role="switch"
                        aria-checked={entry.enabled}
                        aria-label={name}
                        // aria-disabled, not disabled: a disabled button drops focus while the toggle saves.
                        aria-disabled={busy === entry.spec || undefined}
                        onClick={() => { void toggle(entry); }}
                        className="flex rounded-full aria-disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                      >
                        <SwitchTrack on={entry.enabled} />
                      </button>
                    )}
                  />
                );
              })}
              {short ? (
                <SettingsRow
                  label={`${hidden} more model${hidden === 1 ? "" : "s"}`}
                  description={onCount > 0 ? "Only the models that are on show here." : "No model of this provider is on."}
                  control={(
                    <button
                      type="button"
                      className={SETTINGS_QUIET_BUTTON}
                      onClick={() => setShowAll((prev) => ({ ...prev, [g.provider]: true }))}
                    >
                      Show all
                    </button>
                  )}
                />
              ) : null}
            </SettingsGroup>
          );
        })
      )}
    </SettingsPage>
  );
}
