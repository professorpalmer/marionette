import { useCallback, useEffect, useState } from "react";
import { api, type Config } from "../lib/api";
import {
  parseUpdateAvailability,
  updatePillLabel,
  versionLabel,
  type UpdateAvailability,
} from "../lib/updateAvailability";
import { SETTINGS_QUIET_BUTTON, SettingsGroup, SettingsPage, SettingsRow } from "./SettingsLayout";

/** The part of the Electron update bridge this page uses (preload.cjs `updates`). */
type UpdateBridge = {
  check: () => Promise<unknown>;
  openReleases?: () => Promise<unknown>;
};

function updateBridge(): UpdateBridge | null {
  const ipc = (window as { harnessIPC?: { updates?: Partial<UpdateBridge> } }).harnessIPC;
  const updates = ipc?.updates;
  if (!updates || typeof updates.check !== "function") return null;
  return { check: updates.check, openReleases: updates.openReleases };
}

type UpdateState =
  | { kind: "no-bridge" }
  | { kind: "checking" }
  | { kind: "current"; branch: string }
  | { kind: "ready"; update: UpdateAvailability }
  | { kind: "busy" }
  | { kind: "failed"; message: string };

/** One update check result, read as the update banner reads it. */
function readUpdateCheck(payload: unknown): UpdateState {
  const res = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  if (res.busy) return { kind: "busy" };
  const found = parseUpdateAvailability(res);
  if (found) return { kind: "ready", update: found };
  if (typeof res.error === "string" && res.error.trim()) return { kind: "failed", message: res.error.trim() };
  return { kind: "current", branch: typeof res.branch === "string" && res.branch ? res.branch : "main" };
}

function updateText(state: UpdateState): { state: string; help: string; risk?: boolean } {
  switch (state.kind) {
    case "no-bridge":
      return { state: "Not available", help: "Only the desktop app checks for updates." };
    case "checking":
      return { state: "Checking...", help: "The app checks for updates in the background." };
    case "current":
      return { state: "Up to date", help: `This copy has the latest commit on ${state.branch}.` };
    case "ready":
      return {
        state: updatePillLabel(state.update),
        help: state.update.kind === "release"
          ? "Click the update pill in the status bar to install it."
          : "The version does not change. Click the update pill in the status bar to get them.",
      };
    case "busy":
      return { state: "Update in progress", help: "The updater is busy. Check again when it stops." };
    case "failed":
      return { state: "Check failed", help: state.message, risk: true };
    default: {
      const exhaustive: never = state;
      return exhaustive;
    }
  }
}

/** Settings About page: what runs now, and whether an update is ready. */
export default function AboutSettingsPage() {
  const [config, setConfig] = useState<Config | null>(null);
  const [configError, setConfigError] = useState("");
  const [bridge] = useState(updateBridge);
  const [update, setUpdate] = useState<UpdateState>(() => (bridge ? { kind: "checking" } : { kind: "no-bridge" }));

  useEffect(() => {
    let cancelled = false;
    api.config()
      .then((cfg) => { if (!cancelled) setConfig(cfg); })
      .catch(() => { if (!cancelled) setConfigError("Could not read the backend."); });
    return () => { cancelled = true; };
  }, []);

  const check = useCallback(() => {
    if (!bridge) return;
    setUpdate({ kind: "checking" });
    bridge.check()
      .then((res) => setUpdate(readUpdateCheck(res)))
      .catch((err: unknown) => setUpdate({ kind: "failed", message: err instanceof Error ? err.message : String(err) }));
  }, [bridge]);

  useEffect(() => { check(); }, [check]);

  const unknown = configError || (config ? "Unknown" : "Reading...");
  const version = config?.package_version ? versionLabel(config.package_version) : unknown;
  const commit = config?.checkout_sha ? config.checkout_sha.slice(0, 8) : unknown;
  const puppetmaster = config ? (config.puppetmaster_version ? versionLabel(config.puppetmaster_version) : "Not found") : unknown;
  const updateLine = updateText(update);

  return (
    <SettingsPage
      title="About"
      description="Marionette runs any model as the pilot. Puppetmaster runs the workers."
      testId="about-settings"
    >
      <SettingsGroup title="This copy">
        <SettingsRow
          testId="about-version"
          label="Marionette"
          description="The release that runs now."
          state={version}
        />
        <SettingsRow
          testId="about-commit"
          label="Checkout commit"
          description="The git commit of the app source."
          state={<span className="font-mono select-all" title={config?.checkout_sha || undefined}>{commit}</span>}
        />
        <SettingsRow
          testId="about-puppetmaster"
          label="Puppetmaster"
          description="The engine that runs workers and flows."
          state={puppetmaster}
        />
      </SettingsGroup>

      <SettingsGroup title="Updates">
        <SettingsRow
          testId="about-update"
          label="Updates"
          description={<span className={updateLine.risk ? "text-risk" : undefined}>{updateLine.help}</span>}
          state={updateLine.state}
          control={bridge ? (
            <button
              type="button"
              className={SETTINGS_QUIET_BUTTON}
              disabled={update.kind === "checking"}
              onClick={check}
            >
              Check
            </button>
          ) : undefined}
        />
        {bridge?.openReleases ? (
          <SettingsRow
            testId="about-releases"
            label="Release notes"
            description="The latest release on GitHub."
            control={(
              <button
                type="button"
                className={SETTINGS_QUIET_BUTTON}
                onClick={() => { void bridge.openReleases?.(); }}
              >
                Open
              </button>
            )}
          />
        ) : null}
      </SettingsGroup>
    </SettingsPage>
  );
}
