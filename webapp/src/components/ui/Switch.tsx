/** Visual track + knob for a role="switch" control. The caller owns the button,
 *  so a whole row can be the switch (ModelsSettingsPage) or just the track. */
export function SwitchTrack({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      data-switch-track={on ? "on" : "off"}
      className={`relative inline-block shrink-0 w-9 h-5 rounded-full transition-colors ${on ? "bg-accent/80" : "bg-edge"}`}
    >
      <span
        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full transition-transform ${
          on ? "translate-x-4 bg-txt" : "translate-x-0 bg-muted"
        }`}
      />
    </span>
  );
}

/** A track that is its own button, for a Settings row. `label` names the setting. */
export function Switch({
  on,
  onChange,
  label,
  disabled,
  testId,
}: {
  on: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  testId?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      data-testid={testId}
      onClick={() => onChange(!on)}
      className="inline-flex rounded-full disabled:opacity-30"
    >
      <SwitchTrack on={on} />
    </button>
  );
}
