/** Resolve Agent Plugin install sources: absolute path, git, https, or GitHub. */

export type PluginSourceKind = "path" | "git" | "https" | "github";

export type ResolvedPluginSource = {
  kind: PluginSourceKind;
  raw: string;
  path?: string;
  cloneUrl?: string;
  owner?: string;
  repo?: string;
  ref?: string;
};

export class PluginSourceError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PluginSourceError";
  }
}

const WINDOWS_ABS = /^[A-Za-z]:[\\/]/;
const GIT_SSH = /^(?:git@|ssh:\/\/git@|git:\/\/)/i;
const GITHUB_HOST = /^(?:www\.)?github\.com$/i;
const GITHUB_SHORTHAND =
  /^(?:github:)?([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\/([A-Za-z0-9._-]+?)(?:\.git)?(?:[@#](.+))?$/;

export function isAbsolutePluginPath(value: string): boolean {
  const raw = (value || "").trim();
  return raw.startsWith("/") || WINDOWS_ABS.test(raw);
}

function parseGitRemote(cloneUrl: string): { owner?: string; repo?: string } {
  const match = /(?:[:/])([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?$/.exec(
    cloneUrl.replace(/\/+$/, ""),
  );
  if (!match) return {};
  return { owner: match[1], repo: match[2].replace(/\.git$/i, "") };
}

function splitHashRef(raw: string): { base: string; ref: string } {
  const hash = raw.lastIndexOf("#");
  if (hash <= 0) return { base: raw, ref: "" };
  return { base: raw.slice(0, hash), ref: decodeURIComponent(raw.slice(hash + 1)) };
}

export function resolvePluginSource(input: string): ResolvedPluginSource {
  const raw = (input || "").trim();
  if (!raw) {
    throw new PluginSourceError("plugin source is required");
  }
  const scheme = raw.split(":", 1)[0]?.toLowerCase() || "";
  if (scheme === "file" || scheme === "javascript" || scheme === "data" || scheme === "ftp") {
    throw new PluginSourceError("unsupported plugin source scheme");
  }
  if (isAbsolutePluginPath(raw)) {
    return { kind: "path", raw, path: raw };
  }
  if (GIT_SSH.test(raw)) {
    const { base, ref } = splitHashRef(raw);
    return { kind: "git", raw, cloneUrl: base, ref, ...parseGitRemote(base) };
  }
  if (/^https?:\/\//i.test(raw)) {
    return resolveHttpSource(raw);
  }
  if (raw.includes("\\") || raw.startsWith(".") || raw.split("/").length !== 2) {
    throw new PluginSourceError(
      "plugin source must be an absolute path, git URL, https URL, or GitHub source",
    );
  }
  const gh = raw.match(GITHUB_SHORTHAND);
  if (!gh) {
    throw new PluginSourceError(
      "plugin source must be an absolute path, git URL, https URL, or GitHub source",
    );
  }
  const owner = gh[1];
  const repo = gh[2].replace(/\.git$/i, "");
  const ref = gh[3] || "";
  return {
    kind: "github",
    raw,
    owner,
    repo,
    ref,
    cloneUrl: `https://github.com/${owner}/${repo}.git`,
  };
}

function resolveHttpSource(raw: string): ResolvedPluginSource {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new PluginSourceError("invalid plugin source URL");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new PluginSourceError("unsupported plugin source scheme");
  }
  const refFromHash = decodeURIComponent((url.hash || "").replace(/^#/, ""));
  if (GITHUB_HOST.test(url.hostname)) {
    const parts = url.pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
    if (parts.length < 2) {
      throw new PluginSourceError("GitHub source must be owner/repo");
    }
    const owner = parts[0];
    const repo = parts[1].replace(/\.git$/i, "");
    let ref = refFromHash;
    if ((parts[2] === "tree" || parts[2] === "commit") && parts[3]) {
      ref = ref || parts[3];
    }
    return {
      kind: "github",
      raw,
      owner,
      repo,
      ref,
      cloneUrl: `https://github.com/${owner}/${repo}.git`,
    };
  }
  const path = url.pathname.toLowerCase();
  const kind = path.endsWith(".git") ? "git" : "https";
  return {
    kind,
    raw,
    cloneUrl: `${url.origin}${url.pathname}${url.search}`,
    ref: refFromHash,
    ...parseGitRemote(`${url.origin}${url.pathname}`),
  };
}

export function pluginInstallPayload(input: string): {
  source: ResolvedPluginSource;
  path?: string;
} {
  const source = resolvePluginSource(input);
  return source.kind === "path" ? { source, path: source.path } : { source };
}
