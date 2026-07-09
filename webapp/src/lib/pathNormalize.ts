/** Normalize a filesystem path for equality checks across Windows slash/case drift.

Mirrors harness `_norm_path` semantics closely enough for UI matching:
lowercase drive letter + forward slashes. Does not require Node `path` so it
runs in the browser test harness.
*/
export function normalizeRepoPath(path: string): string {
  if (!path) return "";
  let p = path.replace(/\\/g, "/").trim();
  // Lowercase Windows drive letter (C:/foo -> c:/foo) without forcing full
  // lowercase on the rest (POSIX paths can be case-sensitive; we still fold
  // for Windows-style matching via a second compare when needed).
  if (/^[A-Za-z]:\//.test(p)) {
    p = p.charAt(0).toLowerCase() + p.slice(1);
  }
  // Collapse duplicate slashes and strip trailing slash (except root "c:/").
  p = p.replace(/\/+/g, "/");
  if (p.length > 3 && p.endsWith("/")) p = p.slice(0, -1);
  return p;
}

/** True when two repo roots refer to the same directory under UI matching rules. */
export function repoPathsEqual(a: string, b: string): boolean {
  if (!a || !b) return false;
  const na = normalizeRepoPath(a);
  const nb = normalizeRepoPath(b);
  if (na === nb) return true;
  // Windows: fold case for the whole path (normcase).
  return na.toLowerCase() === nb.toLowerCase();
}
