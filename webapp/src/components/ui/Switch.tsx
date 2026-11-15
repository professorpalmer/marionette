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
