import type { Settings } from "../lib/api";

/** Existing Settings keys surfaced by the Opt-ins section. No new flags. */
export const SETTINGS_OPT_IN_KEYS = [
  "auto_distill",
  "hash_edit_enabled",
  "reviewEditsBeforeApply",
  "autoVerify",
] as const;

export type SettingsOptInKey = (typeof SETTINGS_OPT_IN_KEYS)[number];

type OptInRow = {
  key: SettingsOptInKey;
  label: string;
  summary: string;
  help: string;
  defaultOn: boolean;
};

const OPT_INS: OptInRow[] = [
  {
    key: "auto_distill",
    label: "Auto-Distill",
    summary: "Propose skills/rules after task",
    help: "When enabled, PM proposes pending skill/rule candidates automatically on task completion.",
    defaultOn: false,
  },
  {
    key: "hash_edit_enabled",
    label: "Hash-Anchored Edits",
    summary: "Hash-anchored edits (experimental)",
    help: "When on, the agent may apply edits anchored by content hashes instead of line numbers.",
    defaultOn: false,
  },
  {
    key: "reviewEditsBeforeApply",
    label: "Review Edits",
    summary: "Review edits before applying",
    help: "When on, agent edits are held for your per-hunk approval instead of auto-applying.",
    defaultOn: false,
  },
  {
    key: "autoVerify",
    label: "Auto-Verify Edits",
    summary: "Check edits and self-correct",
    help: "After the agent edits files, run a fast project check (typecheck / syntax on the changed files) and let it self-correct in the same turn before handing back.",
    defaultOn: true,
  },
];

export function settingsOptInOn(
  settings: Settings,
  key: SettingsOptInKey,
  defaultOn: boolean,
): boolean {
  const value = settings[key];
  return typeof value === "boolean" ? value : defaultOn;
}

/** Settings Opt-ins: read/write existing keys only. No new settings backend. */
export default function SettingsOptIns({
  settings,
  onUpdate,
  saving = false,
}: {
  settings: Settings;
  onUpdate: (partial: Partial<Settings>) => void | Promise<void>;
  saving?: boolean;
}) {
  return (
    <div className="space-y-3" data-testid="settings-opt-ins">
      <div>
        <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
          Opt-ins
        </label>
        <p className="text-[10px] text-muted">
          Existing Settings switches. Writes the current keys only.
        </p>
      </div>
      {OPT_INS.map((row) => {
        const on = settingsOptInOn(settings, row.key, row.defaultOn);
        return (
          <div key={row.key} className="space-y-1.5">
            <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
              {row.label}
            </label>
            <button
              type="button"
              data-testid={`settings-opt-in-${row.key}`}
              onClick={() => onUpdate({ [row.key]: !on })}
              disabled={saving}
              className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
                on
                  ? "bg-accent/10 border-accent/30 text-accent"
                  : "bg-panel2 border-edge text-muted"
              } disabled:opacity-50`}
            >
              <span className="font-medium text-[11px]">{row.summary}</span>
              <span className="text-[10px] uppercase font-bold tracking-wider">
                {on ? "on" : "off"}
              </span>
            </button>
            <p className="text-[10px] text-muted">{row.help}</p>
          </div>
        );
      })}
    </div>
  );
}
