import { useState } from "react";
import { KeyRound, X } from "lucide-react";
import { TITLEBAR_TRAFFIC_PAD_PX } from "../lib/titlebarSafe";

// Keyless-state nudge. Marionette ships on the 'agentic' engine -- edits and
// swarms route directly through the user's own provider keys, with Puppetmaster
// picking the model live from its registry. Until a key is visible, agentic
// can't route, so this thin, dismissible strip points the user at Settings.
//
// Distinct from the one-time first-run provider connect screen: this is the
// persistent "you're still keyless" reminder that reappears each launch until a
// key is added (Add key reopens that screen; saving a key fires
// harness-config-changed -> App refetches -> workers_ready flips true -> this
// unmounts on its own).
export default function ProviderKeyBanner({
  onAddKey,
  variant = "keyless",
}: {
  onAddKey: () => void;
  variant?: "keyless" | "workers";
}) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  const workersOnly = variant === "workers";

  return (
    // Fixed px paddingLeft clears macOS traffic lights (hiddenInset). Rem-based
    // Tailwind pl-24 shrinks with the responsive root font-size on resize.
    <div
      data-testid="provider-key-banner"
      data-variant={variant}
      className="flex items-center gap-2.5 pr-4 py-1.5 bg-accent/10 border-b border-accent/25 text-[11.5px] text-txt select-none shrink-0"
      style={{ paddingLeft: TITLEBAR_TRAFFIC_PAD_PX }}
    >
      <KeyRound size={13} className="text-accent shrink-0" />
      <span className="font-medium">
        {workersOnly
          ? "Chat works. Add an API key for swarms."
          : "Add a provider key to run real analysis."}
      </span>
      <span className="text-muted hidden sm:inline">
        {workersOnly
          ? "Pilot-only login cannot drive workers. Paste a Full stack key (OpenRouter, Codex, …) — no Cursor install."
          : "Marionette routes pilots and workers through your own keys -- add one and it works out of the box."}
      </span>
      <div className="flex-1" />
      <button
        onClick={onAddKey}
        className="px-2.5 py-0.5 rounded-md bg-accent text-panel font-semibold hover:brightness-110 transition text-[11px]"
      >
        Add key
      </button>
      <button
        onClick={() => setDismissed(true)}
        title="Dismiss (reappears next launch until a key is added)"
        className="p-1 rounded text-muted hover:text-txt hover:bg-edge/40 transition"
      >
        <X size={13} />
      </button>
    </div>
  );
}
