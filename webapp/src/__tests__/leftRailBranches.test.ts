import { describe, expect, it } from "vitest";
import {
  filterBranchWorkspaces,
  isStaleLocalReleaseBranch,
} from "../components/leftRailBranches";
import type { Workspace } from "../lib/api";

function row(
  name: string,
  opts?: Partial<Workspace>,
): Workspace {
  return {
    name,
    branch: name,
    active: false,
    ...opts,
  };
}

describe("leftRailBranches stale release filter", () => {
  it("hides leftover local-only release/v0.9.* without a live worktree", () => {
    const rows = [
      row("main", { active: true }),
      row("dev"),
      row("release/v0.9.308"),
      row("release/v0.9.318", { worktree_path: "/tmp/live-wt" }),
      row("release/v0.9.348"),
      row("feature"),
    ];
    const origin = new Set(["main", "dev", "release/v0.9.348"]);
    const names = filterBranchWorkspaces(rows, origin).map((r) => r.name);
    expect(names).toEqual(["main", "dev", "release/v0.9.348", "feature"]);
    expect(names).not.toContain("release/v0.9.308");
    expect(names).not.toContain("release/v0.9.318");
  });

  it("keeps the active release checkout even when local-only", () => {
    expect(
      isStaleLocalReleaseBranch(row("release/v0.9.331", { active: true }), new Set(["main"])),
    ).toBe(false);
  });

  it("hides inactive release leftovers when origin picture is empty (cache miss safety)", () => {
    expect(isStaleLocalReleaseBranch(row("release/v0.9.310"))).toBe(true);
    expect(isStaleLocalReleaseBranch(row("release/v0.9.310"), new Set())).toBe(true);
    expect(isStaleLocalReleaseBranch(row("main"))).toBe(false);
    expect(isStaleLocalReleaseBranch(row("dev"))).toBe(false);
  });

  it("hides a leftover release worktree the user no longer wants", () => {
    expect(
      isStaleLocalReleaseBranch(
        row("release/v0.9.318", { worktree_path: "C:\\wt\\318" }),
      ),
    ).toBe(true);
  });

  it("hides dest and absorb leftovers not on origin; keeps unpushed features", () => {
    const rows = [
      row("main", { active: true }),
      row("dev"),
      row("dest"),
      row("absorb/marionette-223-scope-labels"),
      row("feat/keep-unpushed"),
    ];
    const origin = new Set(["main", "dev"]);
    const names = filterBranchWorkspaces(rows, origin).map((r) => r.name);
    expect(names).toEqual(["main", "dev", "feat/keep-unpushed"]);
  });
});
