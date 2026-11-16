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

/** Cmd+Shift+D on macOS, Ctrl+Shift+D elsewhere. */
export function isDictationShortcut(event: KeyboardEvent): boolean {
  return (event.metaKey || event.ctrlKey) && event.shiftKey && !event.altKey && event.key.toLowerCase() === "d";
}

export const DICTATION_SHORTCUT_LABEL =
  typeof navigator !== "undefined" && /Mac OS X|Macintosh/.test(navigator.userAgent) ? "Cmd+Shift+D" : "Ctrl+Shift+D";

/**
 * Voice dictation into the message box. Spoken text goes in at the cursor and
 * changes as the engine revises it. It never sends, unless the user turned on
 * "send" by voice and the dictation ends with that word.
 */
export function useDictation(target: DictationTarget) {
  const settings = useVoiceSettings();
  const [state, setState] = useState<DictationState>({ phase: "idle" });
  const session = useRef<DictationSession | null>(null);
  const anchor = useRef<DictationAnchor>({ before: "", after: "" });
  // The value dictation last wrote. A different value means the user typed.
  const written = useRef<string | null>(null);
  // The part of the spoken text that is already inside `anchor`.
  const consumed = useRef(0);
  const generation = useRef(0);
  const targetRef = useRef(target);
  targetRef.current = target;

  // How much spoken text produced `written`.
  const writtenSpoken = useRef(0);

  // Set by the box's input event: the user typed while dictation ran.
  const userEdited = useRef(false);

  const write = (spoken: string): string => {
    const box = targetRef.current.textarea();
    if (box && userEdited.current) {
      // Keep the user's edit and go on at their cursor.
      userEdited.current = false;
      anchor.current = anchorAt(box.value, box.selectionStart, box.selectionEnd);
      consumed.current = writtenSpoken.current;
    }
    const next = spliceDictation(anchor.current, spoken.slice(consumed.current));
    written.current = next.value;
    writtenSpoken.current = spoken.length;
    targetRef.current.apply(next.value, next.caret);
    return next.value;
  };

  const start = async () => {
    const box = targetRef.current.textarea();
    const value = box?.value ?? "";
    anchor.current = anchorAt(value, box?.selectionStart ?? value.length, box?.selectionEnd ?? value.length);
    written.current = null;
    consumed.current = 0;
    writtenSpoken.current = 0;
    userEdited.current = false;
    const run = ++generation.current;
    setState({ phase: "starting" });
    try {
      const next = await startDictation(settings.engine, settings.language, {
        onText: (text) => {
          if (run !== generation.current) return;
          write(text);
        },
        onError: (error) => {
          if (run !== generation.current) return;
          session.current = null;
          setState({ phase: "error", message: error.message, micDenied: error.code === "mic_denied" });
        },
      });
      if (run !== generation.current) {
        next.cancel();
        return;
      }
      session.current = next;
      setState({ phase: "listening" });
    } catch (error) {
      if (run !== generation.current) return;
      const failure = error instanceof DictationError ? error : new DictationError("engine_failed", String(error));
      setState({ phase: "error", message: failure.message, micDenied: failure.code === "mic_denied" });
    }
  };

  const stop = async () => {
    const current = session.current;
    if (!current) return;
    session.current = null;
    const run = generation.current;
    setState({ phase: "finishing" });
    try {
      const final = await current.stop();
      if (run !== generation.current) return;
      const command = settings.sendOnPhrase ? stripSendPhrase(final) : { text: final, send: false };
      const value = final || command.send ? write(command.text) : written.current;
      setState({ phase: "idle" });
      targetRef.current.textarea()?.focus();
      if (command.send && value !== null) targetRef.current.send(value);
    } catch (error) {
      if (run !== generation.current) return;
      const message = error instanceof Error ? error.message : String(error);
      setState({ phase: "error", message, micDenied: false });
    }
  };

  const toggle = () => {
    if (state.phase === "listening") void stop();
    else if (state.phase === "idle" || state.phase === "error") void start();
  };

  const dismiss = () => {
    if (state.phase === "error") setState({ phase: "idle" });
  };

  const toggleRef = useRef(toggle);
  toggleRef.current = toggle;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!isDictationShortcut(event)) return;
      event.preventDefault();
      toggleRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const active = state.phase === "starting" || state.phase === "listening" || state.phase === "finishing";
  useEffect(() => {
    if (!active) return;
    const box = targetRef.current.textarea();
    if (!box) return;
    // React writes the dictated value without an input event, so this fires only for the user.
    const onInput = () => { userEdited.current = true; };
    box.addEventListener("input", onInput);
    return () => box.removeEventListener("input", onInput);
  }, [active]);

  useEffect(() => () => {
    generation.current += 1;
    session.current?.cancel();
    session.current = null;
  }, []);

  return { state, toggle, dismiss, engine: settings.engine };
}
