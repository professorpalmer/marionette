/**
 * Last-good git lists across project flips. Blanking before reload made
 * BRANCHES / SourceControl look cold even when we already knew the repo.
 */

export type GitStatusSnapshot = {
  files: { status: string; path: string }[];
  branches: { name: string; active: boolean }[];
};

export function gitStatusPaintOnRepoChange(cached?: GitStatusSnapshot): {
  kind: "keep" | "blank";
  snapshot?: GitStatusSnapshot;
} {
  if (cached) return { kind: "keep", snapshot: cached };
  return { kind: "blank" };
}
