import type { Workspace } from "../lib/api";

/**
 * Client-side mirror of harness.workspaces._is_stale_local_release.
 * Hides leftover local-only release/v0.9.* even when a stale SWR cache or
 * older API payload still lists them. Keep main/dev, the active checkout,
 * origin-backed names (when known). Leftover release worktrees are hidden.
 */
export function isStaleLocalReleaseBranch(
  row: Pick<Workspace, "name" | "active" | "worktree_path">,
  originBranches?: Iterable<string> | null,
): boolean {
  const name = String(row.name || "");
  if (!name.startsWith("release/v0.9.")) return false;
  if (row.active) return false;
  // Leftover release worktrees stay on disk; hide them on the rail unless
  // they are the active checkout or still on origin.
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
