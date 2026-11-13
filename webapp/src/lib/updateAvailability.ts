/** What the update check found, parsed once at the IPC boundary.
 *
 * - `release`: a different version is ready (new tag on the tracked branch, or a
 *   packaged installer). This earns the banner in the chat card.
 * - `commits`: the tracked branch moved, but its version string is the one that
 *   runs now (a source run between releases). Only the status bar pill shows it.
 */
export type UpdateAvailability =
  | { kind: "release"; version: string; behind: number; branch: string }
  | { kind: "commits"; behind: number; branch: string; version: string };

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function count(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

function bare(version: string): string {
  return version.replace(/^v/i, "");
}

export function parseUpdateAvailability(payload: unknown): UpdateAvailability | null {
  if (!payload || typeof payload !== "object") return null;
  const res = payload as Record<string, unknown>;
  if (!res.available && !res.downloaded) return null;
  const latest = text(res.latest);
  const current = text(res.current) || text(res.shellVersion) || text(res.currentVersion);
  const behind = count(res.behind);
  const branch = text(res.branch) || "main";
  const installer = !!(res.downloaded || res.packagedAvailable || res.installerUpdateRequired);
  const newVersion = latest !== "" && bare(latest) !== bare(current);
  if (newVersion || installer || behind === 0) {
    return { kind: "release", version: latest, behind, branch };
  }
  return { kind: "commits", behind, branch, version: current };
}

/** A release announced by the packaged updater's progress event. */
export function releaseFromProgress(version: unknown): UpdateAvailability {
  return { kind: "release", version: text(version), behind: 0, branch: "main" };
}

export function versionLabel(version: string): string {
  if (!version) return "A new version";
  return version.startsWith("v") ? version : `v${version}`;
}

/** Short status bar text: "v0.9.664 ready" or "2 new commits". */
export function updatePillLabel(update: UpdateAvailability): string {
  switch (update.kind) {
    case "release":
      return update.version ? `${versionLabel(update.version)} ready` : "update ready";
    case "commits":
      return `${update.behind} new commit${update.behind === 1 ? "" : "s"}`;
    default: {
      const exhaustive: never = update;
      return exhaustive;
    }
  }
}

export function updatePillTitle(update: UpdateAvailability): string {
  switch (update.kind) {
    case "release":
      return `${versionLabel(update.version)} of Marionette is ready. Click to update and relaunch.`;
    case "commits": {
      const commits = `${update.behind} commit${update.behind === 1 ? "" : "s"}`;
      const same = update.version ? ` Version ${versionLabel(update.version)} does not change.` : "";
      return `${commits} behind ${update.branch}.${same} Click to pull and relaunch.`;
    }
    default: {
      const exhaustive: never = update;
      return exhaustive;
    }
  }
}
