import { useState } from "react";
import { KeyRound, X } from "lucide-react";
import { useTitlebarLeftPad } from "../lib/useTitlebarLeftPad";

// Workers-gap notice: the pilot runs (a plan login or a pilot-only key) but
// no key can drive workers, so swarms cannot start. With no model at all, the
// composer model slot and the empty session own the "Connect a model" action,
// so this strip does not show. Saving a key fires harness-config-changed, App
// refetches, workers_ready flips true and this strip unmounts on its own.
export default function ProviderKeyBanner({ onAddKey }: { onAddKey: () => void }) {
  const [dismissed, setDismissed] = useState(false);
  const [leftPad, stripRef] = useTitlebarLeftPad();
  if (dismissed) return null;

  return (
    <div
      ref={stripRef}
      data-top-strip=""
      data-testid="provider-key-banner"
      className="flex items-center gap-2.5 pr-4 py-1.5 bg-accent/10 border-b border-accent/25 text-ui-11.5 text-txt select-none shrink-0"
      style={{ paddingLeft: leftPad }}
    >
      <KeyRound size={13} className="text-accent shrink-0" />
      <span className="font-medium">
        Chat works. Add an API key for swarms.
      </span>
      <span className="text-muted hidden sm:inline">
        Pilot-only login cannot drive workers. Paste a Full stack key (OpenRouter, Codex, …) — no Cursor install.
      </span>
      <div className="flex-1" />
      <button
        onClick={onAddKey}
        className="px-2.5 py-0.5 rounded-md bg-accent text-panel font-semibold hover:brightness-110 transition text-ui-11"
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
