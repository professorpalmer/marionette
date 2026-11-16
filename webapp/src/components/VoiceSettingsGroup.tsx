import { useEffect, useState } from "react";
import { getJSON } from "../lib/transport";
import { openMicrophoneSettings, voiceIpc } from "../lib/dictationEngine";
import { DICTATION_SHORTCUT_LABEL } from "../lib/useDictation";
import {
  VOICE_ENGINES,
  VOICE_LANGUAGES,
  useVoiceSettings,
  writeVoiceSettings,
  type VoiceEngine,
} from "../lib/voiceSettings";
import { SETTINGS_QUIET_BUTTON, SettingsGroup, SettingsRow } from "./SettingsLayout";
import SettingsSwitch from "./SettingsSwitch";

type EngineStatus =
  | { kind: "checking" }
  | { kind: "ready"; detail: string | null; micDenied: boolean }
  | { kind: "unavailable"; reason: string };

const SELECT = "bg-panel border border-edge rounded px-1.5 py-0.5 text-ui-11 text-txt focus:outline-none focus:border-accent";

async function readEngineStatus(engine: VoiceEngine): Promise<EngineStatus> {
  switch (engine) {
    case "macos": {
      const ipc = voiceIpc();
      if (!ipc) return { kind: "unavailable", reason: "On-device dictation needs the Marionette desktop app." };
      const status = await ipc.status();
      if (!status.available) return { kind: "unavailable", reason: status.reason || "Not available on this Mac." };
      const micDenied = status.microphone === "denied" || status.microphone === "restricted";
      return { kind: "ready", detail: micDenied ? "Microphone access is off" : null, micDenied };
    }
    case "whisper": {
      const status = await getJSON<{ whisper?: { available?: boolean; reason?: string | null; model?: string | null } }>("/api/voice/status");
      if (!status.whisper?.available) return { kind: "unavailable", reason: status.whisper?.reason || "whisper.cpp is not set up." };
      return { kind: "ready", detail: status.whisper.model ?? null, micDenied: false };
    }
    default: {
      const _exhaustive: never = engine;
      return _exhaustive;
    }
  }
}

/** Voice dictation rows on the General page. */
export default function VoiceSettingsGroup() {
  const voice = useVoiceSettings();
  const [status, setStatus] = useState<EngineStatus>({ kind: "checking" });

  useEffect(() => {
    let live = true;
    setStatus({ kind: "checking" });
    readEngineStatus(voice.engine)
      .then((next) => { if (live) setStatus(next); })
      .catch(() => { if (live) setStatus({ kind: "unavailable", reason: "The engine status did not load." }); });
    return () => { live = false; };
  }, [voice.engine]);

  const engineHelp = VOICE_ENGINES.find((e) => e.value === voice.engine)?.help;
  return (
    <SettingsGroup
      title="Voice"
      description={`Use the mic button next to Send, or ${DICTATION_SHORTCUT_LABEL}: press once to start and again to finish, or hold the keys while you talk. Enter finishes and sends. Audio stays on this computer.`}
      testId="voice-settings"
    >
      <SettingsRow
        testId="voice-row-engine"
        label="Dictation engine"
        description={status.kind === "unavailable" ? <span className="text-warn">{status.reason}</span> : engineHelp}
        state={status.kind === "checking" ? "Checking..." : status.kind === "ready" ? (status.detail ?? "Ready") : "Not available"}
        control={
          <select
            aria-label="Dictation engine"
            value={voice.engine}
            onChange={(e) => {
              const engine = VOICE_ENGINES.find((item) => item.value === e.target.value)?.value;
              if (engine) writeVoiceSettings({ engine });
            }}
            className={SELECT}
          >
            {VOICE_ENGINES.map((engine) => (
              <option key={engine.value} value={engine.value}>{engine.label}</option>
            ))}
          </select>
        }
      >
        {status.kind === "ready" && status.micDenied ? (
          <button type="button" className={SETTINGS_QUIET_BUTTON} onClick={openMicrophoneSettings}>
            Open Microphone settings
          </button>
        ) : null}
      </SettingsRow>
      <SettingsRow
        testId="voice-row-language"
        label="Language"
        description="The language you speak."
        control={
          <select
            aria-label="Dictation language"
            value={voice.language}
            onChange={(e) => {
              const language = VOICE_LANGUAGES.find((item) => item.value === e.target.value)?.value;
              if (language) writeVoiceSettings({ language });
            }}
            className={SELECT}
          >
            {VOICE_LANGUAGES.map((language) => (
              <option key={language.value} value={language.value}>{language.label}</option>
            ))}
          </select>
        }
      />
      <SettingsRow
        testId="voice-row-send"
        label={"Send when I say “send”"}
        description={"End a dictation with the word “send” to send the message. Off: dictation only writes text."}
        control={
          <SettingsSwitch
            on={voice.sendOnPhrase}
            label="Send when I say send"
            testId="voice-switch-send"
            onChange={(next) => { writeVoiceSettings({ sendOnPhrase: next }); }}
          />
        }
      />
    </SettingsGroup>
  );
}
