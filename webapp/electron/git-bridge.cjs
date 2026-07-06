// Native git bridge for the source-control pane. Adapted from the Hermes Agent
// desktop git-scm.cjs pattern (MIT, Nous Research): shell out to git for status,
// diff, and branch list. Read-only operations.
const { execFile } = require("node:child_process");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");

function git(repo, args) {
  return new Promise((resolve) => {
    execFile("git", ["-C", repo, ...args], { maxBuffer: 10_000_000, windowsHide: true }, (err, stdout, stderr) => {
      if (err) return resolve({ ok: false, error: stderr || String(err) });
      resolve({ ok: true, out: stdout });
    });
  });
}

function registerGitBridge(ipcMain) {
  ipcMain.handle("git:status", async (_e, repo) => {
    const r = await git(repo, ["status", "--porcelain=v1", "-b"]);
    if (!r.ok) return r;
    const lines = r.out.split("\n").filter(Boolean);
    let branch = "";
    const files = [];
    for (const line of lines) {
      if (line.startsWith("## ")) { branch = line.slice(3).split("...")[0]; continue; }
      files.push({ status: line.slice(0, 2), path: line.slice(3) });
    }
    return { ok: true, branch, files };
  });

  ipcMain.handle("git:diff", async (_e, repo, file) => {
    const args = ["diff", "--no-color"];
    if (file) args.push("--", file);
    return git(repo, args);
  });

  ipcMain.handle("git:branches", async (_e, repo) => {
    const r = await git(repo, ["branch", "--format=%(refname:short)\t%(HEAD)"]);
    if (!r.ok) return r;
    const branches = r.out.split("\n").filter(Boolean).map((l) => {
      const [name, head] = l.split("\t");
      return { name, active: head === "*" };
    });
    return { ok: true, branches };
  });

  ipcMain.handle("git:stageFile", async (_e, repo, file) => {
    return git(repo, ["add", "--", file]);
  });

  ipcMain.handle("git:unstageFile", async (_e, repo, file) => {
    return git(repo, ["restore", "--staged", "--", file]);
  });

  ipcMain.handle("git:stageAll", async (_e, repo) => {
    return git(repo, ["add", "-A"]);
  });

  ipcMain.handle("git:unstageAll", async (_e, repo) => {
    return git(repo, ["reset", "HEAD"]);
  });

  ipcMain.handle("git:commit", async (_e, repo, message) => {
    if (!message || typeof message !== "string" || message.trim() === "") {
      return { ok: false, error: "empty commit message" };
    }
    return git(repo, ["commit", "-m", message]);
  });

  ipcMain.handle("git:diffStaged", async (_e, repo, file) => {
    const args = ["diff", "--cached", "--no-color"];
    if (file) args.push("--", file);
    return git(repo, args);
  });

  ipcMain.handle("git:applyHunk", async (_e, repo, patchText, reverse) => {
    const filename = `git-hunk-${crypto.randomBytes(16).toString("hex")}.patch`;
    const tmpfile = path.join(os.tmpdir(), filename);
    try {
      await fs.writeFile(tmpfile, patchText, "utf8");
      const args = ["apply", "--cached"];
      if (reverse) {
        args.push("--reverse");
      }
      args.push("--unidiff-zero", tmpfile);
      return await git(repo, args);
    } catch (err) {
      return { ok: false, error: String(err) };
    } finally {
      try {
        await fs.unlink(tmpfile);
      } catch (e) {
        // ignore
      }
    }
  });
}

module.exports = { registerGitBridge };
