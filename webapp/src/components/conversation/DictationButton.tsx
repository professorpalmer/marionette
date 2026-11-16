import { Loader2, Mic } from "lucide-react";
import type { DictationState } from "../../lib/useDictation";
import { DICTATION_SHORTCUT_LABEL } from "../../lib/useDictation";
import { openMicrophoneSettings } from "../../lib/dictationEngine";

const QUIET = "bg-panel2/60 border-edge/60 text-faint hover:text-muted hover:border-edge2";
const LISTENING = "bg-risk/15 border-risk/40 text-risk";

function title(state: DictationState): string {
  switch (state.phase) {
    case "idle":
    case "error":
      return `Dictate: speak and the text goes in at the cursor (${DICTATION_SHORTCUT_LABEL})`;
    case "starting":
      return "Opening the microphone...";
    case "listening":
      return `Listening. Click or press ${DICTATION_SHORTCUT_LABEL} to stop. Nothing is sent.`;
    case "finishing":
      return "Writing the last words...";
    default: {
      const _exhaustive: never = state;
      return _exhaustive;
    }
  }
}

/** Mic button in the composer send row. One click starts, the next one stops. */
export function DictationButton({ state, onToggle }: { state: DictationState; onToggle: () => void }) {
  const listening = state.phase === "listening";
  const busy = state.phase === "starting" || state.phase === "finishing";
  return (
    <button
      type="button"
      onClick={onToggle}
      // A press keeps focus in the message box, so the cursor stays where the text goes.
      onMouseDown={(event) => event.preventDefault()}
      disabled={busy}
      aria-pressed={listening}
      aria-label={listening ? "Stop dictation" : "Start dictation"}
      title={title(state)}
      data-state={state.phase}
      className={`px-2 h-[20px] rounded-md border text-ui-10.5 font-medium flex items-center gap-1 transition disabled:opacity-60 disabled:cursor-default ${listening ? LISTENING : QUIET}`}
    >
      {busy ? <Loader2 size={10} className="animate-spin" aria-hidden="true" /> : <Mic size={10} aria-hidden="true" />}
      {listening ? <span className="composer-toolbar-send-label">Listening</span> : null}
    </button>
  );
}

/** One line under the message box when dictation fails, with a fix when there is one. */
export function DictationError({
  state,
  onDismiss,
}: {
  state: DictationState;
  onDismiss: () => void;
}) {
  if (state.phase !== "error") return null;
  return (
    <div role="alert" className="text-ui-11 text-risk px-3 pt-1 flex items-center gap-2" data-testid="dictation-error">
      <span className="min-w-0">{state.message}</span>
      {state.micDenied ? (
        <button type="button" onClick={openMicrophoneSettings} className="shrink-0 underline hover:text-txt">
          Open Microphone settings
        </button>
      ) : null}
      <button type="button" onClick={onDismiss} className="shrink-0 text-faint hover:text-muted" aria-label="Dismiss dictation error">
        Dismiss
      </button>
    </div>
  );
}
