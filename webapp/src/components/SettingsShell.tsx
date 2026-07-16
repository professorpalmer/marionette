import { useEffect, useState } from "react";
import { X, Cpu, SlidersHorizontal, ShieldCheck, Zap, Bell, Wrench, Info } from "lucide-react";
import ModelsSettingsPage from "./ModelsSettingsPage";
import SettingsPane, { type SettingsSection } from "./SettingsPane";

type PageId = "models" | SettingsSection | "about";

const NAV: { id: PageId; label: string; icon: any }[] = [
  { id: "models", label: "Models", icon: Cpu },
  { id: "general", label: "General", icon: SlidersHorizontal },
  { id: "safety", label: "Safety", icon: ShieldCheck },
  { id: "providers", label: "Accounts & Keys", icon: Zap },
  { id: "notifications", label: "Notifications", icon: Bell },
  { id: "advanced", label: "Advanced", icon: Wrench },
  { id: "about", label: "About", icon: Info },
];

// Full-screen settings overlay: left sidebar nav + routed content area
// (Cursor/Hermes pattern). The title bar reserves space on the left so the
// "Settings" label clears the macOS traffic-light window controls.
export default function SettingsShell({
  onClose,
  onOpenWizard,
}: {
  onClose: () => void;
  onOpenWizard: () => void;
}) {
  const [page, setPage] = useState<PageId>("models");

  // Escape always closes settings -- a keyboard escape hatch so a missed click
  // on the X (e.g. a busy main thread during a swarm) can never trap the user
  // behind this full-window overlay. Capture phase so it wins over inner inputs.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 bg-bg flex flex-col">
      {/* top bar -- pl-20 clears the macOS traffic lights so the title is not obscured */}
      <div className="flex items-center justify-between pl-20 pr-4 h-11 border-b border-edge/40 shrink-0">
        <span className="text-[13px] font-semibold text-txt">Settings</span>
        <button
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
          {page === "about" && (
            <div className="max-w-2xl text-[12px] text-muted">
              <h2 className="text-[15px] font-semibold text-txt mb-2">About</h2>
              <p>Marionette -- a desktop AI coding harness over Puppetmaster durable state.</p>
            </div>
          )}
          {page !== "models" && page !== "about" && (
            <SettingsPane onOpenWizard={onOpenWizard} section={page as SettingsSection} />
          )}
        </div>
      </div>
    </div>
  );
}
