/**
 * Leaf label for a workspace path (Home for the harness home dir, else basename).
 */
export function workspaceLeafName(path: string, home?: string): string {
  if (!path) return "";
  if (
    home
    && path.replace(/\\/g, "/").toLowerCase() === home.replace(/\\/g, "/").toLowerCase()
  ) {
    return "Home";
  }
  if (/[/\\]\.pmharness[/\\]home$/i.test(path.replace(/\\/g, "/"))) return "Home";
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
}
