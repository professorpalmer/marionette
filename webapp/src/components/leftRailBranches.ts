import type { Workspace } from "../lib/api";

/**
 * Client-side mirror of harness.workspaces._is_stale_local_release.
 * Hides leftover local-only release/v0.9.* even when a stale SWR cache or
 * older API payload still lists them. Keep main/dev, the active checkout,
 * origin-backed names (when known), and a live worktree path.
 */
export function isStaleLocalReleaseBranch(
  row: Pick<Workspace, "name" | "active" | "worktree_path">,
  originBranches?: Iterable<string> | null,
): boolean {
  const name = String(row.name || "");
  if (!name.startsWith("release/v0.9.")) return false;
  if (row.active) return false;
  const wt = String(row.worktree_path || "").trim();
  // Live worktree path present → keep (dir check is best-effort in the UI).
  if (wt) return false;
  if (originBranches) {
    const remote = originBranches instanceof Set
      ? originBranches
      : new Set(Array.from(originBranches));
    if (remote.has(name)) return false;
  }
  // No origin picture or origin already dropped this release → hide.
  return true;
}

/** Filter BRANCHES rows so phantom release leftovers cannot paint. */
export function filterBranchWorkspaces(
  rows: Workspace[],
  originBranches?: Iterable<string> | null,
): Workspace[] {
  return (rows || []).filter((row) => !isStaleLocalReleaseBranch(row, originBranches));
}
