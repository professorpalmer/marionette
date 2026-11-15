import { useEffect, useState } from "react";
import { Plus, Search, X } from "lucide-react";
import {
  api,
  type Settings,
} from "../lib/api";
import {
  readSettingsSnapshot,
  writeSettingsSnapshot,
} from "./settingsSnapshot";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import { SettingsCollapse } from "./SettingsCollapse";

export type SettingsSection = "general" | "notifications" | "plugins";

export {
  toSafeSettingsSnapshot,
  clearSettingsSnapshot,
  readSettingsSnapshot,
  writeSettingsSnapshot,
} from "./settingsSnapshot";

export default function SettingsPane({ section = "general" }: { onOpenWizard: () => void; section?: SettingsSection }) {
  const show = (s: SettingsSection) => section === s;
  // Settings search/filter: when a query is active we search ACROSS all sections
  // (ignore the current-section gate) and hide individual settings whose label +
  // help text do not contain the query. Empty query = today's behavior exactly.
  const [filter, setFilter] = useState("");
  const q = filter.trim().toLowerCase();
  const matches = (text: string) => !q || text.toLowerCase().includes(q);
  // Section gate: normally only the active section shows; while searching, all
  // sections are eligible so cross-section matches surface.
  const active = (s: SettingsSection) => (q ? true : show(s));
  // Track whether any setting rendered, to show a "no matches" hint.
  let anyShown = false;
  // gate() combines the section visibility with the keyword filter and records
  // whether anything survived so we can render a "no matches" line.
  const gate = (s: SettingsSection, keywords: string) => {
    const ok = active(s) && matches(keywords) && settings !== null;
    if (ok) anyShown = true;
    return ok;
  };
  // Note: search granularity is PER-SETTING already -- every gate() call below
  // carries its own keyword string (label + synonyms) for one logical setting,
  // so typing e.g. "timeout" or "distill" filters to just that control across
  // all sections. No separate per-item wrapper is needed.
  const [settings, setSettings] = useState<Settings | null>(() => readSettingsSnapshot());
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const errorNotice = usePanelNotice(error || null);
  
  };

  useEffect(() => {
    // Revalidate in background; cached snapshot paints immediately (no blink).
    api.settings()
      .then((next) => {
        setSettings(next);
        writeSettingsSnapshot(next);
        setError("");
      })
      .catch((err) => {
        if (!readSettingsSnapshot()) {
          setError("Failed to load settings");
        }
        console.error(err);
      });
  }, []);

  const searchActive = filter.trim().length > 0;
  useEffect(() => {

  }, [section, searchActive]);

  const update = async (partial: Partial<Settings> & { api_key?: string; clear_api_key?: boolean }) => {
    if (!settings) return;
    setSaving(true);
    setStatus("");
    setError("");
    try {
      const updated = await api.updateSettings(partial);
      setSettings(updated);
      writeSettingsSnapshot(updated);
      setStatus("saved");
      // Mirror PilotPicker swap: settings can change driver / reach / keys, so
      // the picker and other listeners must refetch without a full reload.
      window.dispatchEvent(new Event("harness-config-changed"));
      const timer = setTimeout(() => setStatus(""), 2000);
      return () => clearTimeout(timer);
    } catch (err: any) {
      setError(err?.error || "Failed to update settings");
    } finally {
      setSaving(false);
    }
  };

  const canRenderWithoutSettings = searchActive;
  if (!settings && !canRenderWithoutSettings) {
    return (
      <div className="flex flex-col h-full text-ui-12 p-4 text-faint">
        {errorNotice ? errorNotice : "Loading settings..."}
      </div>
    );
  }

  return (
    <div className="text-ui-12 max-w-3xl">
      {/* Floating save/error toast: fixed to the bottom-right so it overlays
          instead of inserting a block at the top that shoves every setting down
          (the reflow was the annoyance). Auto-dismiss handled by the callers
          that setStatus(""). pointer-events-none so it never blocks controls. */}
      {(status || error) && (
        <div className="fixed bottom-4 right-4 z-50 pointer-events-none flex items-center gap-2
                        px-3 py-1.5 rounded-lg border shadow-lg bg-panel2/95 backdrop-blur
                        border-edge">
          {status && <span className="text-good text-ui-11 font-medium">{status}</span>}
          {errorNotice && <span className="text-risk text-ui-11 font-medium">{errorNotice}</span>}
        </div>
      )}

      {/* Settings search: sticky at the top so it stays reachable while the
          dense settings list scrolls. Uses a FULLY OPAQUE background (bg-panel)
          and a high z-index so scrolled settings pass BEHIND it instead of
          bleeding through the bar. -mx-8 pt-6 -mt-6 offsets the shell's px-8/py-6
          scroll-container padding so the bar spans edge-to-edge and covers the
          gap above it (no sliver of content shows over the top). */}
      <div className="sticky -top-6 z-30 -mx-8 px-8 pt-6 pb-2 mb-2 bg-panel border-b border-edge">
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 text-faint" size={13} />
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search settings..."
            className="w-full bg-panel border border-edge rounded text-ui-11 text-txt
                       pl-7 pr-7 py-1.5 outline-none focus:border-accent placeholder:text-faint"
          />
          {filter && (
            <button
              type="button"
              onClick={() => setFilter("")}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-txt"
            >
              <X size={13} />
            </button>
          )}
        </div>
      </div>

      <div className="space-y-4">
        </div>

        </>)}
        {q && !anyShown && (
          <p className="text-ui-11 text-muted">No settings match "{filter.trim()}".</p>
        )}
      </div>
    </div>
  );
}
