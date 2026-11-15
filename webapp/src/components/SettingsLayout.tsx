import type { ReactNode } from "react";

/**
 * The parts every Settings page is built from, in the shape the Accounts page
 * set: a page title with one line of purpose, then groups of rows. A row is one
 * setting: its name, one line of help, its current state, and one control.
 */

export function SettingsPage({
  title,
  description,
  children,
  testId,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <div className="max-w-2xl space-y-5" data-testid={testId}>
      <header className="space-y-1">
        <h2 className="text-ui-15 font-semibold text-txt">{title}</h2>
        {description ? <p className="text-ui-12 text-muted">{description}</p> : null}
      </header>
      {children}
    </div>
  );
}

export function SettingsGroup({
  title,
  description,
  children,
  testId,
}: {
  title?: string;
  description?: string;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section className="space-y-1.5" data-testid={testId}>
      {title ? <h3 className="text-ui-11 font-medium uppercase tracking-wide text-faint">{title}</h3> : null}
      {description ? <p className="text-ui-11 text-faint">{description}</p> : null}
      <ul className="divide-y divide-edge/30 border border-edge/40 rounded-md bg-panel2/40">{children}</ul>
    </section>
  );
}

export function SettingsRow({
  label,
  description,
  state,
  control,
  children,
  testId,
}: {
  label: ReactNode;
  description?: ReactNode;
  /** The current value or status in words, when the control does not show it. */
  state?: ReactNode;
  control?: ReactNode;
  /** Extra content under the row, such as an open editor or a result line. */
  children?: ReactNode;
  testId?: string;
}) {
  return (
    <li className="px-3 py-2" data-testid={testId}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-ui-12 text-txt">{label}</div>
          {description ? <div className="text-ui-11 text-faint">{description}</div> : null}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {state ? <span className="text-ui-11 text-muted">{state}</span> : null}
          {control}
        </div>
      </div>
      {children ? <div className="mt-1.5">{children}</div> : null}
    </li>
  );
}

/** Button styles shared by Settings rows, the same as the Accounts page. */
export const SETTINGS_BUTTON = "rounded px-2.5 py-0.5 font-medium text-ui-10 border disabled:opacity-30 transition-colors";
export const SETTINGS_PRIMARY_BUTTON = `${SETTINGS_BUTTON} bg-accent/15 hover:bg-accent/25 text-accent border-accent/30`;
export const SETTINGS_QUIET_BUTTON = "text-muted hover:text-txt text-ui-10 disabled:opacity-30";
