import { useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { useTitlebarLeftPad } from "../lib/useTitlebarLeftPad";

/** Startup key-store failures: distinct from the keyless ProviderKeyBanner. */
export default function KeyBootstrapBanner({
  issues,
  onOpenSettings,
}: {
  issues: { step: string; message: string }[];
  onOpenSettings: () => void;
}) {
  const [dismissed, setDismissed] = useState(false);
  const [leftPad, stripRef] = useTitlebarLeftPad();
  if (dismissed || !issues.length) return null;

  return (
    <div
      ref={stripRef}
      data-top-strip=""
      data-testid="key-bootstrap-banner"
      className="flex items-center gap-2.5 pr-4 py-1.5 bg-warn/10 border-b border-warn/30 text-ui-11.5 text-txt select-none shrink-0"
      style={{ paddingLeft: leftPad }}
    >
      <AlertTriangle size={13} className="text-warn shrink-0" />
      <span className="font-medium">A provider key did not save on startup.</span>
      <span className="text-muted hidden sm:inline">
        Open Settings → API keys to confirm stored credentials. The server kept running.
      </span>
      <div className="flex-1" />
      <button
        onClick={onOpenSettings}
        className="px-2.5 py-0.5 rounded-md bg-warn/80 text-panel font-semibold hover:brightness-110 transition text-ui-11"
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
