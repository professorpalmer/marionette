import { useEffect, useRef, useState } from "react";
import { DictationError, startDictation, type DictationSession } from "./dictationEngine";
import { anchorAt, spliceDictation, stripSendPhrase, type DictationAnchor } from "./dictationText";
import { useVoiceSettings } from "./voiceSettings";

export type DictationState =
  | { phase: "idle" }
  | { phase: "starting" }
  | { phase: "listening" }
  | { phase: "finishing" }
  | { phase: "error"; message: string; micDenied: boolean };

export type DictationTarget = {
  /** The message box. Dictation reads its value and cursor. */
  textarea: () => HTMLTextAreaElement | null;
  /** Put a new message value in the box, with the caret at `caret`. */
  apply: (value: string, caret: number) => void;
  /** Send the message box as it is after the last `apply`. */
  send: (value: string) => void;
};

/** A shortcut press held this long is hold-to-talk: the release stops. */
export const HOLD_TO_TALK_MS = 400;

/** Cmd+Shift+D on macOS, Ctrl+Shift+D elsewhere. */
export function isDictationShortcut(event: KeyboardEvent): boolean {
  return (event.metaKey || event.ctrlKey) && event.shiftKey && !event.altKey && event.key.toLowerCase() === "d";
}

function releasesShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLowerCase();
  return key === "d" || key === "meta" || key === "control" || key === "shift";
}

export const DICTATION_SHORTCUT_LABEL =
  typeof navigator !== "undefined" && /Mac OS X|Macintosh/.test(navigator.userAgent) ? "Cmd+Shift+D" : "Ctrl+Shift+D";

/**
 * Voice dictation into the message box. Spoken text goes in at the cursor and
 * changes as the engine revises it. The box is read-only while dictation runs.
 * It sends only when the user presses Enter to finish, or says "send" with
 * "Send when I say send" on.
 */
export function useDictation(target: DictationTarget) {
  const settings = useVoiceSettings();
  const [state, setState] = useState<DictationState>({ phase: "idle" });
  // Microphone loudness, 0 to 1, while listening.
  const [level, setLevel] = useState(0);
  const session = useRef<DictationSession | null>(null);
  const anchor = useRef<DictationAnchor>({ before: "", after: "" });
  const written = useRef<string | null>(null);
  const generation = useRef(0);
  // A stop asked for before the engine was ready: { send } or null.
  const pendingStop = useRef<{ send: boolean } | null>(null);
  const targetRef = useRef(target);
  targetRef.current = target;

  const takePendingStop = (): { send: boolean } | null => {
    const stop = pendingStop.current;
    pendingStop.current = null;
    return stop;
  };

  const write = (spoken: string): string => {
    const next = spliceDictation(anchor.current, spoken);
    written.current = next.value;
    targetRef.current.apply(next.value, next.caret);
    return next.value;
  };

  const finish = async (send: boolean) => {
    const current = session.current;
    if (!current) {
      if (state.phase === "starting") pendingStop.current = { send };
      return;
    }
    session.current = null;
    const run = generation.current;
    setState({ phase: "finishing" });
    setLevel(0);
    try {
      const final = await current.stop();
      if (run !== generation.current) return;
      const command = settings.sendOnPhrase ? stripSendPhrase(final) : { text: final, send: false };
      const value = final || command.send ? write(command.text) : written.current;
      setState({ phase: "idle" });
      targetRef.current.textarea()?.focus();
      if ((send || command.send) && value !== null) targetRef.current.send(value);
    } catch (error) {
      if (run !== generation.current) return;
      const message = error instanceof Error ? error.message : String(error);
      setState({ phase: "error", message, micDenied: false });
    }
  };

  const start = async () => {
    const box = targetRef.current.textarea();
    const value = box?.value ?? "";
    anchor.current = anchorAt(value, box?.selectionStart ?? value.length, box?.selectionEnd ?? value.length);
    written.current = null;
    pendingStop.current = null;
    const run = ++generation.current;
    setState({ phase: "starting" });
    try {
      const next = await startDictation(settings.engine, settings.language, {
        onText: (text) => {
          if (run === generation.current) write(text);
        },
        onLevel: (value) => {
          if (run === generation.current) setLevel(value);
        },
        onError: (error) => {
          if (run !== generation.current) return;
          session.current = null;
          setLevel(0);
          setState({ phase: "error", message: error.message, micDenied: error.code === "mic_denied" });
        },
      });
      if (run !== generation.current) {
        next.cancel();
        return;
      }
      session.current = next;
      setState({ phase: "listening" });
      const stopNow = takePendingStop();
      if (stopNow) await finish(stopNow.send);
    } catch (error) {
      if (run !== generation.current) return;
      const failure = error instanceof DictationError ? error : new DictationError("engine_failed", String(error));
      setState({ phase: "error", message: failure.message, micDenied: failure.code === "mic_denied" });
    }
  };

  const toggle = () => {
    if (state.phase === "listening" || state.phase === "starting") void finish(false);
    else if (state.phase === "idle" || state.phase === "error") void start();
  };

  const dismiss = () => {
    if (state.phase === "error") setState({ phase: "idle" });
  };

  const active = state.phase === "starting" || state.phase === "listening" || state.phase === "finishing";

  /** Keys in the message box while dictation runs: Enter finishes and sends, Escape finishes. */
  const handleKey = (event: { key: string; shiftKey: boolean; preventDefault: () => void }): boolean => {
    if (!active) return false;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void finish(true);
      return true;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      void finish(false);
      return true;
    }
    return false;
  };

  const latest = useRef({ toggle, finish, phase: state.phase });
  latest.current = { toggle, finish, phase: state.phase };
  useEffect(() => {
    // A press that starts dictation and is held is hold-to-talk; a short press toggles.
    let heldFrom: number | null = null;
    const onDown = (event: KeyboardEvent) => {
      if (!isDictationShortcut(event)) return;
      event.preventDefault();
      if (event.repeat) return;
      const idle = latest.current.phase === "idle" || latest.current.phase === "error";
      heldFrom = idle ? Date.now() : null;
      latest.current.toggle();
    };
    const onUp = (event: KeyboardEvent) => {
      if (heldFrom === null || !releasesShortcut(event)) return;
      const held = Date.now() - heldFrom;
      heldFrom = null;
      if (held >= HOLD_TO_TALK_MS) void latest.current.finish(false);
    };
    window.addEventListener("keydown", onDown);
    window.addEventListener("keyup", onUp);
    return () => {
      window.removeEventListener("keydown", onDown);
      window.removeEventListener("keyup", onUp);
    };
  }, []);

  useEffect(() => () => {
    generation.current += 1;
    session.current?.cancel();
    session.current = null;
  }, []);

  return { state, level, active, toggle, dismiss, handleKey, engine: settings.engine };
}
