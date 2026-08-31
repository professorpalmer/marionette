import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X, Cpu, HardDrive, SlidersHorizontal, ShieldCheck, Zap, Bell, Wrench, Info, Puzzle } from "lucide-react";
import ModelsSettingsPage from "./ModelsSettingsPage";
import LocalModelsSettingsPage from "./LocalModelsSettingsPage";
import SettingsPane, { type SettingsSection } from "./SettingsPane";
import PluginsPane from "./PluginsPane";
import { TITLEBAR_TRAFFIC_PAD_SM_PX } from "../lib/titlebarSafe";
import { useOverlayFocus } from "../lib/overlayFocus";

type PageId = "models" | "local-models" | SettingsSection | "about";

const NAV: { id: PageId; label: string; icon: any }[] = [
  { id: "models", label: "Models", icon: Cpu },
  { id: "general", label: "General", icon: SlidersHorizontal },
  { id: "local-models", label: "Local Models", icon: HardDrive },
  { id: "safety", label: "Safety", icon: ShieldCheck },
  { id: "providers", label: "Accounts & Keys", icon: Zap },
  { id: "notifications", label: "Notifications", icon: Bell },
  { id: "plugins", label: "Plugins", icon: Puzzle },
  { id: "advanced", label: "Advanced", icon: Wrench },
  { id: "about", label: "About", icon: Info },
];

const PAGE_IDS = new Set<string>(NAV.map((n) => n.id));

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
  onOpenWizard,
  initialPage,
}: {
  onClose: () => void;
  onOpenWizard: () => void;
  initialPage?: PageId;
}) {
  const [page, setPage] = useState<PageId>(
    () => initialPage || takePendingSettingsPage() || "models",
  );
  const shellRef = useRef<HTMLDivElement>(null);

  useOverlayFocus(true, shellRef, {
    onClose,
  });

  // Add key / hotkeys can request Accounts & Keys while Settings is already open.
  useEffect(() => {
    const onPage = (e: Event) => {
      const next = (e as CustomEvent).detail;
      if (typeof next === "string" && PAGE_IDS.has(next)) {
        pendingSettingsPage = null;
        setPage(next as PageId);
      }
    };
    window.addEventListener("harness-settings-page", onPage as EventListener);
    return () => window.removeEventListener("harness-settings-page", onPage as EventListener);
  }, []);

  return createPortal(
    <div
      ref={shellRef}
      role="dialog"
      aria-modal="true"
      aria-label="Settings"
      className="fixed inset-0 z-[80] bg-bg flex flex-col"
      data-testid="settings-shell"
    >
      {/* top bar -- fixed px pad clears macOS traffic lights (not rem) */}
      <div
        data-testid="settings-shell-titlebar"
        className="flex items-center justify-between pr-4 h-11 border-b border-edge/40 shrink-0"
        style={{ paddingLeft: TITLEBAR_TRAFFIC_PAD_SM_PX }}
      >
        <span className="text-[13px] font-semibold text-txt">Settings</span>
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
        {/* sidebar -- compact width, tight row spacing */}
        <div className="w-44 shrink-0 border-r border-edge/40 py-2 px-1.5 flex flex-col gap-0.5 overflow-y-auto">
          {NAV.map((item) => {
            const Icon = item.icon;
            const active = page === item.id;
            return (
              <button
                key={item.id}
                onClick={() => setPage(item.id)}
                className={`flex items-center gap-2 px-2 py-1.5 rounded-md text-[12px] text-left transition
                  ${active ? "bg-panel2 text-txt font-medium" : "text-muted hover:text-txt hover:bg-panel2/50"}`}
              >
                <Icon size={13} className={active ? "text-accent" : "text-faint"} />
                {item.label}
              </button>
            );
          })}
        </div>

        {/* content */}
        <div className="flex-1 min-w-0 overflow-y-auto px-8 py-6">
          {page === "models" && <ModelsSettingsPage />}
          {page === "local-models" && <LocalModelsSettingsPage />}
          {page === "plugins" && (
            <div className="max-w-2xl">
              <h2 className="text-[15px] font-semibold text-txt mb-3">Plugins</h2>
              <PluginsPane />
            </div>
          )}
          {page === "about" && (
            <div className="max-w-2xl text-[12px] text-muted">
              <h2 className="text-[15px] font-semibold text-txt mb-2">About</h2>
              <p>Marionette -- a desktop AI coding harness over Puppetmaster durable state.</p>
            </div>
          )}
          {page !== "models" && page !== "local-models" && page !== "about" && page !== "plugins" && (
            <SettingsPane onOpenWizard={onOpenWizard} section={page as SettingsSection} />
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
