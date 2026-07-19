import { useEffect, useMemo, useState } from "react";
import { Search, RefreshCw, ChevronRight, ChevronDown } from "lucide-react";
import { api, type ModelCatalogEntry } from "../lib/api";

const COLLAPSE_THRESHOLD = 12;
const CATALOG_SNAPSHOT_KEY = "pmharness.models.catalogSnapshot";
let memoryCatalogSnapshot: ModelCatalogEntry[] | null = null;

type ProviderGroup = { provider: string; display: string; items: ModelCatalogEntry[] };

export function clearCatalogSnapshot() {
  memoryCatalogSnapshot = null;
  try {
    localStorage.removeItem(CATALOG_SNAPSHOT_KEY);
  } catch {
    /* ignore */
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
    /* stale/corrupt cache is disposable */
  }
  return [];
}

function writeCatalogSnapshot(catalog: ModelCatalogEntry[]) {
  memoryCatalogSnapshot = catalog;
  try {
    localStorage.setItem(CATALOG_SNAPSHOT_KEY, JSON.stringify({
      catalog,
      savedAt: Date.now(),
    }));
  } catch {
    /* storage pressure must not break Settings */
  }
}

// Models settings page: toggle which provider models appear in the pilot picker
// dropdown (Cursor/Hermes-style). Grouped by provider; only providers with a
// present key are shown. The enabled set is persisted server-side and feeds
// /api/pilot picker via model_visibility.enabled_pilots().
export default function ModelsSettingsPage() {
  const [catalog, setCatalog] = useState<ModelCatalogEntry[]>(readCatalogSnapshot);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(() => catalog.length === 0);
  const [busy, setBusy] = useState<string | null>(null);
  // Explicit user toggles win; otherwise large catalogs (OpenRouter) start
  // collapsed so Anthropic/OpenAI/xAI are reachable without endless scroll.
  const [collapsedProviders, setCollapsedProviders] = useState<Record<string, boolean>>({});

  const load = async (opts?: { refresh?: boolean }) => {
    const hadSnapshot = readCatalogSnapshot().length > 0;
    if (opts?.refresh || !hadSnapshot) setLoading(true);
    try {
      const res = await api.modelCatalog({ refresh: !!opts?.refresh });
      const next = res.catalog || [];
      setCatalog(next);
      writeCatalogSnapshot(next);
    } catch {
      // Stale-while-revalidate: keep the last truthful snapshot instead of
      // flashing an empty page during a transient backend/provider delay.
      setCatalog((prev) => (prev.length === 0 ? [] : prev));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const toggle = async (entry: ModelCatalogEntry) => {
    const next = !entry.enabled;
    setBusy(entry.spec);
    // optimistic
    setCatalog((prev) => {
      const updated = prev.map((c) => (c.spec === entry.spec ? { ...c, enabled: next } : c));
      writeCatalogSnapshot(updated);
      return updated;
    });
    try {
      await api.toggleModel(entry.spec, next);
      // tell the picker to refetch its list
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch {
      // revert on failure
      setCatalog((prev) => {
        const reverted = prev.map((c) => (c.spec === entry.spec ? { ...c, enabled: !next } : c));
        writeCatalogSnapshot(reverted);
        return reverted;
      });
    } finally {
      setBusy(null);
    }
  };

  const q = query.trim().toLowerCase();
  const groups = useMemo(() => {
    const filtered = q
      ? catalog.filter(
          (c) =>
            c.model.toLowerCase().includes(q) ||
            c.provider_display.toLowerCase().includes(q),
        )
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
    return out;
  }, [catalog, q]);

  const enabledCount = catalog.filter((c) => c.enabled).length;

  const defaultCollapsed = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const g of groups) {
      // Search: keep matching groups open. Otherwise collapse huge catalogs
      // (OpenRouter) even when some models are enabled — the header shows
      // "N on" so curation stays visible without scrolling past hundreds of rows.
      map[g.provider] = !q && g.items.length > COLLAPSE_THRESHOLD;
    }
    return map;
  }, [groups, q]);

  const isCollapsed = (provider: string) =>
    collapsedProviders[provider] !== undefined
      ? collapsedProviders[provider]
      : !!defaultCollapsed[provider];

  const toggleGroup = (provider: string) => {
    setCollapsedProviders((prev) => ({
      ...prev,
      [provider]: !isCollapsed(provider),
    }));
  };

  return (
    <div className="max-w-2xl">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-[15px] font-semibold text-txt">Models</h2>
          <p className="text-[11px] text-muted mt-0.5">
            Toggle which models appear in the pilot picker. {enabledCount > 0
              ? `${enabledCount} enabled.`
              : "None curated -- the picker shows every available model."}
          </p>
        </div>
        <button
          onClick={() => load({ refresh: true })}
          title="Refresh live model catalogs (Cursor Agent CLI, Codex, …)"
          className="p-1.5 rounded-md border border-edge/40 text-muted hover:text-txt hover:bg-panel2 transition"
        >
          <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      <div className="flex items-center gap-2 mb-3 px-2.5 py-1.5 rounded-lg bg-panel2 border border-edge/50">
        <Search size={13} className="text-faint shrink-0" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search models or providers"
          className="bg-transparent text-[12px] text-txt placeholder:text-faint outline-none w-full"
        />
      </div>

      {loading && catalog.length === 0 ? (
        <div className="text-[12px] text-faint py-8 text-center">Loading model catalog...</div>
      ) : groups.length === 0 ? (
        <div className="text-[12px] text-faint py-8 text-center">
          No models available. Add a provider key in Providers &amp; Keys first.
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {groups.map((g) => {
            const collapsed = isCollapsed(g.provider);
            const onCount = g.items.filter((i) => i.enabled).length;
            return (
              <div key={g.provider}>
                <button
                  type="button"
                  onClick={() => toggleGroup(g.provider)}
                  className="w-full flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-faint font-semibold mb-1.5 px-1 hover:text-txt transition"
                >
                  {collapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                  <span className="truncate">{g.display}</span>
                  <span className="normal-case tracking-normal font-normal text-faint/80">
                    · {g.items.length}
                    {onCount > 0 ? ` · ${onCount} on` : ""}
                  </span>
                </button>
                {!collapsed && (
                  <div className="flex flex-col rounded-lg border border-edge/40 overflow-hidden">
                    {g.items.map((entry, i) => (
                      <button
                        key={entry.spec}
                        onClick={() => toggle(entry)}
                        disabled={busy === entry.spec}
                        className={`flex items-center justify-between px-3 py-2 text-left transition
                          ${i > 0 ? "border-t border-edge/30" : ""}
                          ${entry.enabled ? "bg-accent/5 hover:bg-accent/10" : "hover:bg-panel2/60"}
                          disabled:opacity-50`}
                      >
                        <span className="min-w-0 flex-1">
                          <span className="text-[12px] text-txt font-mono truncate block">{entry.model}</span>
                        </span>
                        <span
                          className={`shrink-0 ml-3 flex items-center justify-center w-9 h-5 rounded-full transition relative
                            ${entry.enabled ? "bg-accent/80" : "bg-edge"}`}
                        >
                          <span
                            className={`absolute w-4 h-4 rounded-full bg-white transition-transform
                              ${entry.enabled ? "translate-x-2" : "-translate-x-2"}`}
                          />
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
