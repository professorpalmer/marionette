import { useEffect, useState, type KeyboardEvent } from "react";
import { Search, X } from "lucide-react";
import ModelsSettingsPage from "./ModelsSettingsPage";
import LocalModelsSettingsPage from "./LocalModelsSettingsPage";
import GeneralSettingsPage from "./GeneralSettingsPage";
import NotificationsSettingsPage from "./NotificationsSettingsPage";
import SafetySettingsPage from "./SafetySettingsPage";
import AdvancedSettingsPage from "./AdvancedSettingsPage";
import AccountsSettingsPage from "./AccountsSettingsPage";
import PluginsPane from "./PluginsPane";
import ArchivedChatsPane from "./ArchivedChatsPane";
import AboutSettingsPage from "./AboutSettingsPage";
import { PAGE_IDS, matchSettingsPages, type PageId } from "./settingsNav";
import { TITLEBAR_TRAFFIC_PAD_SM_PX } from "../lib/titlebarSafe";
import { Modal } from "./ui/overlay";

// Latched when Add key (or similar) asks for a settings page before SettingsShell
// has mounted. Consumed on mount so a closed right pane still lands correctly.
let pendingSettingsPage: PageId | null = null;

export function focusSettingsPage(page: PageId): void {
  if (!PAGE_IDS.has(page)) return;
  pendingSettingsPage = page;
  window.dispatchEvent(new CustomEvent("harness-settings-page", { detail: page }));
}

function takePendingSettingsPage(): PageId | null {
  const page = pendingSettingsPage;
  pendingSettingsPage = null;
  return page;
}

// Full-screen settings overlay: left sidebar nav + routed content area
// (Cursor/Hermes pattern). The title bar reserves space on the left so the
// "Settings" label clears the macOS traffic-light window controls.
export default function SettingsShell({
  onClose,
  initialPage,
}: {
  onClose: () => void;
  onOpenWizard: () => void;
  initialPage?: PageId;
}) {
  const [page, setPage] = useState<PageId>(
    () => initialPage || takePendingSettingsPage() || "models",
  );
  const [query, setQuery] = useState("");
  const shown = matchSettingsPages(query);
  const search = (next: string) => {
    setQuery(next);
    const first = matchSettingsPages(next)[0];
    if (next.trim() && first) setPage(first.id);
  };
  const onSearchKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Escape" || !query) return;
    // Escape clears the search first. The next Escape closes Settings.
    e.stopPropagation();
    search("");
  };
  // Add key / hotkeys can request Accounts while Settings is already open.
  useEffect(() => {
    const onPage = (e: Event) => {
      const next = (e as CustomEvent).detail;
      if (typeof next === "string" && PAGE_IDS.has(next)) {
        pendingSettingsPage = null;
        setQuery("");
        setPage(next as PageId);
      }
    };
    window.addEventListener("harness-settings-page", onPage as EventListener);
    return () => window.removeEventListener("harness-settings-page", onPage as EventListener);
  }, []);

  return (
    <Modal
      open
      onClose={onClose}
      label="Settings"
      dismissOnOutsidePress={false}
      className="fixed inset-0 z-[80] bg-bg flex flex-col outline-none"
      testId="settings-shell"
    >
      {/* top bar -- fixed px pad clears macOS traffic lights (not rem) */}
      <div
        data-testid="settings-shell-titlebar"
        className="flex items-center justify-between pr-4 h-11 border-b border-edge/40 shrink-0"
        style={{ paddingLeft: TITLEBAR_TRAFFIC_PAD_SM_PX }}
      >
        <span className="text-ui-13 font-semibold text-txt">Settings</span>
        <button
          type="button"
          onClick={onClose}
          title="Close settings"
          className="p-1.5 rounded-md text-muted hover:text-txt hover:bg-panel2 transition"
        >
          <X size={16} />
        </button>
      </div>

      <div className="flex flex-1 min-h-0">
        {/* sidebar -- search, then the pages that match it */}
        <nav aria-label="Settings pages" className="w-44 shrink-0 border-r border-edge/40 py-2 px-1.5 flex flex-col gap-0.5 overflow-y-auto">
          <div className="relative mb-1.5">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-faint pointer-events-none" />
            <input
              type="search"
              value={query}
              onChange={(e) => search(e.target.value)}
              onKeyDown={onSearchKey}
              placeholder="Search settings"
              aria-label="Search settings"
              data-testid="settings-search"
              className="w-full bg-panel2/60 border border-edge/40 rounded-md text-ui-11 text-txt pl-6 pr-2 py-1 outline-none focus:border-accent placeholder:text-faint"
            />
          </div>
          {shown.map((item) => {
            const Icon = item.icon;
            const active = page === item.id;
            return (
              <button
                key={item.id}
                type="button"
                aria-current={active ? "page" : undefined}
                onClick={() => setPage(item.id)}
                className={`flex items-center gap-2 px-2 py-1.5 rounded-md text-ui-12 text-left transition
                  ${active ? "bg-panel2 text-txt font-medium" : "text-muted hover:text-txt hover:bg-panel2/50"}`}
              >
                <Icon size={13} className={active ? "text-accent" : "text-faint"} />
                {item.label}
              </button>
            );
          })}
          {shown.length === 0 ? (
            <p className="px-2 py-1.5 text-ui-11 text-faint" data-testid="settings-search-empty">No page matches.</p>
          ) : null}
        </nav>

        {/* content */}
        <div className="flex-1 min-w-0 overflow-y-auto px-8 py-6">
          {page === "models" && <ModelsSettingsPage />}
          {page === "local-models" && <LocalModelsSettingsPage />}
          {page === "plugins" && <PluginsPane />}
          {page === "archived" && <ArchivedChatsPane onOpenSession={onClose} />}
          {page === "about" && <AboutSettingsPage />}
          {page === "general" && <GeneralSettingsPage />}
          {page === "notifications" && <NotificationsSettingsPage />}
          {page === "safety" && <SafetySettingsPage />}
          {page === "advanced" && <AdvancedSettingsPage />}
          {page === "providers" && <AccountsSettingsPage />}
        </div>
      </div>
    </Modal>
  );
}
