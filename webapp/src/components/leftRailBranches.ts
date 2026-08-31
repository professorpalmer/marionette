import type { Workspace } from "../lib/api";

function originSet(originBranches?: Iterable<string> | null): Set<string> | null {
  if (originBranches == null) return null;
  return originBranches instanceof Set
    ? originBranches
    : new Set(Array.from(originBranches));
}

/**
 * Client-side mirror of harness.workspaces._is_stale_local_workspace.
 * Hides leftover dest / absorb / release/v0.9.* even when a stale SWR cache
 * or older API payload still lists them. Keep main/dev, the active checkout,
 * origin-backed names, and unpushed feature branches.
 */
export function isStaleLocalReleaseBranch(
  row: Pick<Workspace, "name" | "active" | "worktree_path">,
  originBranches?: Iterable<string> | null,
): boolean {
  if (row.active) return false;
  const name = String(row.name || "");
  const remote = originSet(originBranches);
  if (name === "dest" && (remote == null || !remote.has(name))) return true;
  if (name.startsWith("absorb/") && (remote == null || !remote.has(name))) return true;
  if (!name.startsWith("release/v0.9.")) return false;
  if (remote && remote.has(name)) return false;
  return true;
}

/** Filter BRANCHES rows so phantom leftovers cannot paint. */
export function filterBranchWorkspaces(
  rows: Workspace[],
  originBranches?: Iterable<string> | null,
): Workspace[] {
  return (rows || []).filter((row) => !isStaleLocalReleaseBranch(row, originBranches));
}
