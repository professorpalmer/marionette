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
      return `Dictate (${DICTATION_SHORTCUT_LABEL}). Press once to start and again to finish, or hold the keys while you talk.`;
    case "starting":
      return "Starting dictation\u2026";
    case "listening":
      return "Finish dictating. Enter finishes and sends.";
    case "finishing":
      return "Finishing dictation\u2026";
    default: {
      const _exhaustive: never = state;
      return _exhaustive;
    }
  }
}

function label(state: DictationState): string | null {
  switch (state.phase) {
    case "starting":
      return "Starting\u2026";
    case "listening":
      return "Listening\u2026";
    case "finishing":
      return "Finishing\u2026";
    case "idle":
    case "error":
      return null;
    default: {
      const _exhaustive: never = state;
      return _exhaustive;
    }
  }
}

/**
 * Mic button in the composer send row. One click starts, the next one
 * finishes. While it listens, a dot grows with the voice, so the user sees
 * that the microphone hears them.
 */
export function DictationButton({ state, level, onToggle }: { state: DictationState; level: number; onToggle: () => void }) {
  const listening = state.phase === "listening";
  const text = label(state);
  return (
    <button
      type="button"
      onClick={onToggle}
      // A press keeps focus in the message box, so the cursor stays where the text goes.
      onMouseDown={(event) => event.preventDefault()}
      disabled={state.phase === "finishing"}
      aria-pressed={listening || state.phase === "starting"}
      aria-label={listening || state.phase === "starting" ? "Finish dictating" : "Dictate"}
      title={title(state)}
      data-state={state.phase}
      className={`px-2 h-[20px] rounded-md border text-ui-10.5 font-medium flex items-center gap-1 transition disabled:opacity-60 disabled:cursor-default ${text ? LISTENING : QUIET}`}
    >
      {state.phase === "listening" ? (
        <span
          aria-hidden="true"
          data-testid="dictation-level"
          className="inline-block w-[7px] h-[7px] rounded-full bg-risk transition-transform duration-75"
          style={{ transform: `scale(${(0.55 + Math.min(1, level) * 0.75).toFixed(2)})` }}
        />
      ) : text ? (
        <Loader2 size={10} className="animate-spin" aria-hidden="true" />
      ) : (
        <Mic size={10} aria-hidden="true" />
      )}
      {text ? <span className="composer-toolbar-send-label">{text}</span> : null}
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
