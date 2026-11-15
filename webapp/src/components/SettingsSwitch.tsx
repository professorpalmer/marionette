import { SwitchTrack } from "./ui/Switch";

/** The on/off control of a Settings row. The row shows the name and the help. */
export default function SettingsSwitch({
  on,
  label,
  onChange,
  disabled = false,
  testId,
}: {
  on: boolean;
  label: string;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  testId?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      data-testid={testId}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className="rounded-full disabled:opacity-30 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
    >
      <SwitchTrack on={on} />
    </button>
  );
}
