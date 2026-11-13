import { KeyRound, HardDrive } from "lucide-react";
import type { Config } from "../../lib/api";
import { openOnboarding, workspaceName } from "../../lib/sessionNav";
import { focusSettingsPage } from "../SettingsShell";

/** What an empty session needs to know about the shell around it. */
export type EmptySessionContext = {
  /** Workspace folder name, or "" for Home. */
  workspace: string;
  /** No pilot can run: no key, plan sign-in or local model. */
  needsModel: boolean;
};

/** A keyless install still runs a local pilot (`local:` spec, no key). */
export function emptySessionContext(config: Pick<Config, "repo" | "driver" | "pilot_ready"> | null): EmptySessionContext {
  const name = workspaceName(config?.repo);
  return {
    workspace: name === "Home" ? "" : name,
    needsModel: config?.pilot_ready === false && !(config.driver || "").startsWith("local:"),
  };
}

function openLocalModels(): void {
  focusSettingsPage("local-models");
  window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "settings" }));
}

/** Empty / loading placeholders for the transcript feed. The empty session
 *  heading sits at the bottom of the feed, right above the centered composer. */
export default function TranscriptEmptyState({
  transcriptStale,
  itemCount,
  context,
}: {
  transcriptStale: boolean;
  itemCount: number;
  context?: EmptySessionContext;
}) {
  if (itemCount === 0 && !transcriptStale) {
    if (context?.needsModel) {
      return (
        <div data-testid="empty-session-needs-model" className="mt-auto pb-5 text-center">
          <h2 className="text-ui-15 font-semibold text-txt text-balance">Connect a model to start</h2>
          <p className="mt-1.5 text-ui-12.5 text-muted leading-relaxed text-balance">
            Marionette runs on your own models. Add an API key, sign in with a plan, or run a model on this Mac.
          </p>
          <div className="mt-4 flex items-center justify-center gap-2">
            <button
              type="button"
              onClick={openOnboarding}
              className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-lg bg-accent text-panel font-semibold text-ui-12.5 hover:brightness-110 transition"
            >
              <KeyRound size={13} />
              Connect a model
            </button>
            <button
              type="button"
              onClick={openLocalModels}
              className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-edge text-txt text-ui-12.5 hover:bg-panel2/70 transition"
            >
              <HardDrive size={13} className="text-muted" />
              Use a local model
            </button>
          </div>
        </div>
      );
    }
    return (
      <div data-testid="empty-session-heading" className="mt-auto pb-5 text-center">
        <h2 className="text-ui-15 font-semibold text-txt text-balance">
          {context?.workspace ? `What should we do in ${context.workspace}?` : "What should we work on?"}
        </h2>
        <p className="mt-1.5 text-ui-12.5 text-muted">
          The pilot plans, reads the code, sends work to swarms, and explains what it did.
        </p>
      </div>
    );
  }
  // Cold cache miss: keep the seating shell quiet. A "Loading session…" label
  // plus opacity dim was the visible swap blink (worse cold→cold). Prefetch +
  // warm cache still hydrate instantly; this only covers the miss paint.
  if (transcriptStale && itemCount === 0) {
    return (
      <div
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-busy="true"
        aria-label="Loading session"
      />
    );
  }
  return null;
}
