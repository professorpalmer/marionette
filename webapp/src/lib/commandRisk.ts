/**
 * Risk of a one-click Run of a transcript command. "write" commands delete,
 * move, or change files, git state, or processes, or write through a
 * redirect. A Run of a "write" command needs a confirm. The check is on
 * the shape of the text only: an unknown command counts as "read".
 */
export type CommandRisk = "read" | "write";

const WRITE_PROGRAMS = new Set([
  "rm", "rmdir", "mv", "cp", "dd", "mkfs", "shred", "truncate", "unlink", "ln",
  "chmod", "chown", "chgrp", "kill", "pkill", "killall", "sudo", "doas",
  "tee", "install", "rsync", "patch", "touch", "mkdir",
]);

/** git subcommands that change the work tree, the index, refs, or a remote. */
const GIT_WRITE = new Set([
  "reset", "clean", "checkout", "switch", "restore", "rebase", "merge", "commit",
  "push", "pull", "stash", "rm", "mv", "apply", "am", "cherry-pick", "revert",
  "branch", "tag", "worktree", "gc", "prune", "filter-branch", "init", "add",
]);

/** Package managers and runners whose install / remove verbs write. */
const PACKAGE_WRITE = /^(?:npm|pnpm|yarn|bun|pip3?|uv|poetry|cargo|brew|apt(?:-get)?|gem|go)\s+(?:i|install|add|remove|rm|uninstall|un|update|upgrade|publish|sync|lock|get)\b/;

/** Remove quoted text so a quoted ">" or ";" does not count. */
function stripQuoted(cmd: string): string {
  return cmd.replace(/'[^']*'|"(?:\\.|[^"\\])*"/g, "''");
}

/** A redirect to a file. `2>&1`, `>&2` and `> /dev/null` do not write files. */
function writesThroughRedirect(cmd: string): boolean {
  const re = /(\d?)(>>?)(?!&)\s*([^\s;|&]+)?/g;
  for (const m of cmd.matchAll(re)) {
    const target = m[3] || "";
    if (target === "/dev/null" || target === "/dev/stderr" || target === "/dev/stdout") continue;
    return true;
  }
  return false;
}

function segmentRisk(segment: string): CommandRisk {
  const words = segment.trim().split(/\s+/).filter(Boolean);
  // Skip leading env assignments (FOO=1 cmd).
  while (words.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[0])) words.shift();
  if (!words.length) return "read";
  const program = words[0].split("/").pop() || words[0];
  if (WRITE_PROGRAMS.has(program)) return "write";
  if (program === "git") {
    const sub = words.slice(1).find((w) => !w.startsWith("-"));
    if (sub && GIT_WRITE.has(sub)) return "write";
  }
  if ((program === "sed" || program === "perl") && words.some((w) => /^-[a-zA-Z]*i/.test(w))) return "write";
  if (program === "find" && words.some((w) => w === "-delete" || w === "-exec" || w === "-execdir")) return "write";
  if (program === "xargs") return segmentRisk(words.slice(1).filter((w) => !w.startsWith("-")).join(" "));
  if (PACKAGE_WRITE.test([program, ...words.slice(1)].join(" "))) return "write";
  if (program === "docker" && /^(?:rm|rmi|kill|stop|prune|system)$/.test(words[1] || "")) return "write";
  return "read";
}

export function commandRisk(command: string): CommandRisk {
  const text = stripQuoted(String(command || ""));
  if (!text.trim()) return "read";
  if (writesThroughRedirect(text)) return "write";
  // `curl ... | sh` runs downloaded code.
  if (/\|\s*(?:sudo\s+)?(?:ba|z)?sh\b/.test(text)) return "write";
  const segments = text.split(/&&|\|\||;|\||\n/);
  return segments.some((s) => segmentRisk(s) === "write") ? "write" : "read";
}
