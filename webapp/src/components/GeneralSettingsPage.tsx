import { useEffect, useState } from "react";
import { api, type Settings } from "../lib/api";
import { readSettingsSnapshot, writeSettingsSnapshot } from "./settingsSnapshot";
import { SettingsGroup, SettingsPage, SettingsRow } from "./SettingsLayout";
import SettingsSwitch from "./SettingsSwitch";
import VoiceSettingsGroup from "./VoiceSettingsGroup";
import WindowGlassSettings from "./WindowGlassSettings";

type Residual = NonNullable<Settings["compactionResidual"]>;

/** Boolean settings on this page. Each one is one row and one switch. */
type SwitchKey = "outputStyle" | "reviewEditsBeforeApply" | "autoVerify" | "hash_edit_enabled" | "auto_distill" | "jev_enabled";

const SWITCHES: { key: SwitchKey; label: string; help: string }[] = [
  {
    key: "outputStyle",
    label: "Simplified Technical English",
    help: "The pilot and new workers write short, controlled sentences. Code, paths and quotes stay exact.",
  },
  {
    key: "reviewEditsBeforeApply",
    label: "Review edits before they apply",
    help: "The agent holds each edit until you approve it, one hunk at a time.",
  },
  {
    key: "autoVerify",
    label: "Check edits",
    help: "After an edit, a fast type or syntax check runs. The agent fixes errors in the same turn.",
  },
  {
    key: "hash_edit_enabled",
    label: "Hash-anchored edits",
    help: "Experimental. The agent finds the edit place by a content hash, not by a line number.",
  },
  {
    key: "auto_distill",
    label: "Propose skills after a task",
    help: "When a task ends, the agent proposes new skills and rules. You accept or reject each one.",
  },
  {
    key: "jev_enabled",
    label: "Jev skill choice",
    help: "An OpenRouter Decisions call picks which skill to load. It needs an OpenRouter key.",
  },
];

const RESIDUALS: { value: Residual; label: string }[] = [
  { value: "catalog", label: "Catalog" },
  { value: "hybrid", label: "Summary and handle index" },
  { value: "summary", label: "Summary" },
];

/** Defaults are the backend defaults when the key is missing (harness/server.py get_settings_dict). */
export function switchOn(settings: Settings, key: SwitchKey): boolean {
  if (key === "outputStyle") return settings.outputStyle === "ste";
  const value = settings[key];
  return typeof value === "boolean" ? value : key === "autoVerify";
}

function switchPatch(key: SwitchKey, next: boolean): Partial<Settings> {
  if (key === "outputStyle") return { outputStyle: next ? "ste" : "off" };
  return { [key]: next };
}

export default function GeneralSettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(() => readSettingsSnapshot());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    // The cached snapshot paints at once. This read replaces it.
    api.settings()
      .then((next) => {
        setSettings(next);
        writeSettingsSnapshot(next);
        setError("");
      })
      .catch(() => {
        if (!readSettingsSnapshot()) setError("Settings did not load.");
      });
  }, []);

  const update = async (partial: Partial<Settings>) => {
    setSaving(true);
    setError("");
    try {
      const next = await api.updateSettings(partial);
      setSettings(next);
      writeSettingsSnapshot(next);
      // The picker and other readers fetch the config again.
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (err: unknown) {
      const message = err && typeof err === "object" && "error" in err ? String(err.error) : "";
      setError(message || "The change did not save.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <SettingsPage title="General" description="How the agent writes and edits, voice dictation, and how the window looks." testId="general-settings">
      {error ? <p className="text-ui-11 text-risk" role="alert">{error}</p> : null}
      {!settings ? (
        error ? null : <p className="text-ui-11 text-faint">Loading settings...</p>
      ) : (
        <>
          <SettingsGroup title="Agent">
            {SWITCHES.map((row) => {
              const on = switchOn(settings, row.key);
              const idle = row.key === "jev_enabled" && on && settings.jev_ready === false;
              return (
                <SettingsRow
                  key={row.key}
                  testId={`general-row-${row.key}`}
                  label={row.label}
                  description={row.help}
                  state={idle ? "Idle: no OpenRouter key" : undefined}
                  control={
                    <SettingsSwitch
                      on={on}
                      label={row.label}
                      disabled={saving}
                      testId={`general-switch-${row.key}`}
                      onChange={(next) => { void update(switchPatch(row.key, next)); }}
                    />
                  }
                />
              );
            })}
          </SettingsGroup>

          <SettingsGroup title="Compaction">
            <SettingsRow
              testId="general-row-compactionResidual"
              label="Keep after compact"
              description="Catalog keeps files, decisions and the latest story, and finds old slices on demand. The summary choices use a cheaper summarizer when one is set."
              control={
                <select
                  aria-label="Keep after compact"
                  value={settings.compactionResidual || "catalog"}
                  disabled={saving}
                  onChange={(e) => {
                    const value = RESIDUALS.find((r) => r.value === e.target.value)?.value;
                    if (value) void update({ compactionResidual: value });
                  }}
                  className="bg-panel border border-edge rounded px-1.5 py-0.5 text-ui-11 text-txt focus:outline-none focus:border-accent disabled:opacity-30"
                >
                  {RESIDUALS.map((r) => (
                    <option key={r.value} value={r.value}>{r.label}</option>
                  ))}
                </select>
              }
            />
          </SettingsGroup>

          <VoiceSettingsGroup />

          <WindowGlassSettings />

          <SettingsGroup title="This app">
            <SettingsRow
              label="Project folder"
              description={<span className="font-mono select-all break-all">{settings.repo || "None"}</span>}
            />
            <SettingsRow
              label="State folder"
              description={<span className="font-mono select-all break-all">{settings.state_dir || "Temporary, for this session only"}</span>}
            />
          </SettingsGroup>
        </>
      )}
    </SettingsPage>
  );
}
