import { useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { TITLEBAR_TRAFFIC_PAD_PX } from "../lib/titlebarSafe";

/** Startup key-store failures: distinct from the keyless ProviderKeyBanner. */
export default function KeyBootstrapBanner({
  issues,
  onOpenSettings,
}: {
  issues: { step: string; message: string }[];
  onOpenSettings: () => void;
}) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed || !issues.length) return null;

  return (
    <div
      data-testid="key-bootstrap-banner"
      className="flex items-center gap-2.5 pr-4 py-1.5 bg-warn/10 border-b border-warn/30 text-[11.5px] text-txt select-none shrink-0"
      style={{ paddingLeft: TITLEBAR_TRAFFIC_PAD_PX }}
    >
      <AlertTriangle size={13} className="text-warn shrink-0" />
      <span className="font-medium">A provider key did not save on startup.</span>
      <span className="text-muted hidden sm:inline">
        Open Settings → API keys to confirm stored credentials. The server kept running.
      </span>
      <div className="flex-1" />
      <button
        onClick={onOpenSettings}
        className="px-2.5 py-0.5 rounded-md bg-warn/80 text-panel font-semibold hover:brightness-110 transition text-[11px]"
      >
        Open API keys
      </button>
      <button
        onClick={() => setDismissed(true)}
        title="Dismiss"
        className="p-1 rounded text-muted hover:text-txt hover:bg-edge/40 transition"
      >
        <X size={13} />
      </button>
    </div>
  );
}
