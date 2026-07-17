// Transport abstraction -- the seam that keeps pm-harness NOT web-locked.
//
// Every backend interaction goes through this module. Today it uses fetch + SSE
// against the local Python harness server. When we package as an Electron app,
// ONLY this file changes: getJSON/postJSON/stream route through window.harnessIPC
// (preload bridge) instead of HTTP. Components never know the difference.

/**
 * Live SSE payload from /api/chat, /api/auto, /api/run.
 * Chat/auto omit `turn` (ConvEvent); classic /run includes it (SessionEvent).
 * Keep both shapes — do not require `turn` for chat consumers.
 */
export type StreamEvent = { kind: string; data?: any; turn?: number };

/** One retained SSE frame from GET /api/chat/events (mid-turn reattach). */
export type ChatEventFrame = {
  cursor: number;
  kind: string;
  data?: any;
  /** Present only when the source event carried a turn (SessionEvent /run). */
  turn?: number;
};

/** Replay payload for mid-turn SSE reattach (Conversation will consume later). */
export type ChatEventReplay = {
  ok?: boolean;
  missed?: boolean;
  available?: boolean;
  code?: "ring_miss" | "generation_mismatch" | string;
  session_id: string;
  generation: number;
  cursor: number;
  events: ChatEventFrame[];
  retained?: number;
};

/** Build the tokened URL for chat event replay / reattach. */
export function chatEventsPath(opts: {
  session?: string;
  since?: number;
  generation?: number;
} = {}): string {
  const params = new URLSearchParams();
  if (opts.session) params.set("session", opts.session);
  if (opts.since != null) params.set("since", String(opts.since));
  if (opts.generation != null) params.set("generation", String(opts.generation));
  const q = params.toString();
  return withToken(`/api/chat/events${q ? `?${q}` : ""}`);
}

/** Live Electron preload bridge (do not freeze at module import). */
export function getHarnessIpc(): any {
  if (typeof window === "undefined") return null;
  return (window as any).harnessIPC || null;
}

// Per-process auth token (defense-in-depth against unauthenticated localhost
// access). Electron injects window.__HARNESS_TOKEN__; the served web page reads
// it from a meta tag. Host/Origin validation server-side is the primary guard.
function authToken(): string {
  if (typeof window === "undefined") return "";
  const w = window as any;
  if (w.__HARNESS_TOKEN__) return w.__HARNESS_TOKEN__;
  const meta = document.querySelector('meta[name="harness-token"]');
  return (meta && meta.getAttribute("content")) || "";
}

export function withToken(path: string): string {
  const tok = authToken();
  if (!tok) return path;
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(tok);
}

/** True for loopback backend briefly gone (respawn / port flip). */
export function isTransientHarnessConnError(err: unknown): boolean {
  const code = (err as { code?: string; errno?: string } | null)?.code
    || (err as { code?: string; errno?: string } | null)?.errno;
  if (
    code === "ECONNREFUSED"
    || code === "ECONNRESET"
    || code === "EPIPE"
    || code === "ETIMEDOUT"
  ) {
    return true;
  }
  const msg = String((err as { message?: string } | null)?.message || err || "");
  return /ECONNREFUSED|ECONNRESET|socket hang up|EPIPE|ETIMEDOUT/i.test(msg);
}

/** Like getJSON but returns parsed JSON for non-2xx responses instead of throwing. */
export async function getJSONSoft<T = any>(path: string): Promise<T> {
  const bridge = getHarnessIpc();
  if (bridge?.getJSON) return bridge.getJSON(path);
  const r = await fetch(path, { headers: { "X-Harness-Token": authToken() } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok && body && typeof body === "object" && !("ok" in body)) {
    return { ok: false, error: (body as any).error || `${path} -> ${r.status}`, ...body } as T;
  }
  if (!r.ok && (body == null || typeof body !== "object")) {
    return { ok: false, error: `${path} -> ${r.status}` } as T;
  }
  return body as T;
}

export async function getJSON<T = any>(path: string): Promise<T> {
  const bridge = getHarnessIpc();
  if (bridge?.getJSON) return bridge.getJSON(path);
  const r = await fetch(path, { headers: { "X-Harness-Token": authToken() } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

export async function postJSON<T = any>(path: string, body: any): Promise<T> {
  const bridge = getHarnessIpc();
  if (bridge?.postJSON) return bridge.postJSON(path, body);
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Harness-Token": authToken() },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    // Parse the body when present so callers (lease detectors) can require
    // code===lease_exhausted instead of guessing from "... -> 409".
    const parsed = await r.json().catch(() => null);
    if (parsed && typeof parsed === "object") {
      const err = new Error(
        String((parsed as { error?: string }).error || `${path} -> ${r.status}`),
      ) as Error & Record<string, unknown>;
      Object.assign(err, parsed, { status: r.status });
      throw err;
    }
    throw new Error(`${path} -> ${r.status}`);
  }
  return r.json();
}

// NOTE: no deleteJSON here on purpose. The Electron preload bridge only routes
// GET/POST; a DELETE silently falls through to fetch, which cannot reach the
// backend in the desktop app. Deletion endpoints are POST verbs instead.

// Stream server-sent events. Returns a cancel() function. In Electron this maps
// to an IPC event channel; on the web it's EventSource.
export function stream(
  path: string,
  onEvent: (ev: StreamEvent) => void,
  onDone?: () => void,
  onError?: (e: any) => void
): () => void {
  const bridge = getHarnessIpc();
  if (bridge?.stream) return bridge.stream(path, onEvent, onDone, onError);
  const es = new EventSource(withToken(path));
  es.onmessage = (m) => {
    let ev: StreamEvent;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.kind === "done") { es.close(); onDone?.(); return; }
    onEvent(ev);
  };
  es.onerror = (e) => { es.close(); onError?.(e); };
  return () => es.close();
}

// Upload a file (multipart). In Electron a browser File object cannot cross the
// IPC boundary, so we read it into bytes and hand {name, type, bytes} to the main
// process, which POSTs a multipart body to the loopback backend. On the web build
// (real same-origin server) we use a normal multipart fetch.
export async function uploadFile(file: File): Promise<{ path: string; name: string }[]> {
  const bridge = getHarnessIpc();
  if (bridge?.uploadFile) {
    const buf = await file.arrayBuffer();
    return bridge.uploadFile({ name: file.name, type: file.type, bytes: new Uint8Array(buf) });
  }
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/upload", { method: "POST", body: fd, headers: { "X-Harness-Token": authToken() } });
  const j = await r.json();
  return j.saved || [];
}

// Native desktop bridges (file tree + git). Web build returns not-supported.
export const nativeFs = {
  readDir: (dir: string): Promise<{ ok: boolean; nodes?: any[]; error?: string }> => {
    const bridge = getHarnessIpc();
    return bridge?.fs?.readDir
      ? bridge.fs.readDir(dir)
      : Promise.resolve({ ok: false, error: "web build" });
  },
  readFile: (file: string): Promise<{ ok: boolean; content?: string; error?: string }> => {
    const bridge = getHarnessIpc();
    return bridge?.fs?.readFile
      ? bridge.fs.readFile(file)
      : Promise.resolve({ ok: false, error: "web build" });
  },
  revealInFolder: (absPath: string): Promise<{ ok: boolean; error?: string }> => {
    const bridge = getHarnessIpc();
    return bridge?.fs?.revealInFolder
      ? bridge.fs.revealInFolder(absPath)
      : Promise.resolve({ ok: false, error: "web build" });
  },
};

/** OS-specific label for shell.showItemInFolder. */
export function revealInFolderLabel(): string {
  const p = typeof navigator !== "undefined" ? navigator.platform || "" : "";
  if (/Win/i.test(p)) return "Open in File Explorer";
  if (/Mac/i.test(p)) return "Open in Finder";
  return "Reveal in file manager";
}

function looksAbsolutePath(p: string): boolean {
  if (!p) return false;
  if (/^[a-zA-Z]:[\\/]/.test(p)) return true;
  if (p.startsWith("\\\\") || p.startsWith("//")) return true;
  // POSIX absolute (and avoid treating Windows drive-relative as abs)
  if (p.startsWith("/") && !/^[a-zA-Z]:/.test(p)) return true;
  return false;
}

/** Join a workspace-relative path to ``repoRoot``, or return abs paths as-is. */
export function toAbsoluteWorkspacePath(repoRoot: string, relOrAbs: string): string {
  const raw = (relOrAbs || "").trim();
  if (!raw) return repoRoot;
  if (looksAbsolutePath(raw)) return raw;
  const sep = repoRoot.includes("\\") ? "\\" : "/";
  const root = repoRoot.replace(/[\\/]+$/, "");
  const rel = raw.replace(/^[\\/]+/, "").replace(/[\\/]+/g, sep);
  return `${root}${sep}${rel}`;
}

/**
 * Reveal a workspace path in the OS file manager.
 * Prefer Electron ``fs.revealInFolder``; fall back to ``POST /api/file/reveal``
 * so a stale preload (or HTTP-only UI) never toasts the useless "web build".
 */
export async function revealWorkspacePath(
  repoRoot: string,
  relOrAbs: string,
): Promise<{ ok: boolean; error?: string }> {
  if (!repoRoot && !looksAbsolutePath(relOrAbs)) {
    return { ok: false, error: "No open workspace" };
  }
  const abs = toAbsoluteWorkspacePath(repoRoot || "", relOrAbs);
  const bridge = getHarnessIpc();
  if (bridge?.fs?.revealInFolder) {
    try {
      const res = await bridge.fs.revealInFolder(abs);
      if (res && res.ok) return res;
    } catch {
      // fall through to HTTP
    }
  }
  const rel = looksAbsolutePath(relOrAbs)
    ? workspaceRelFromAbs(repoRoot, abs)
    : String(relOrAbs || "").replace(/\\/g, "/");
  try {
    await postJSON("/api/file/reveal", { path: rel || "." });
    return { ok: true };
  } catch (e: any) {
    return { ok: false, error: e?.message || "Could not reveal path" };
  }
}

function workspaceRelFromAbs(repoRoot: string, abs: string): string {
  const root = (repoRoot || "").replace(/[\\/]+$/, "");
  if (!root) return abs.replace(/\\/g, "/");
  const normRoot = root.replace(/\\/g, "/");
  const normAbs = abs.replace(/\\/g, "/");
  const a = normAbs.toLowerCase();
  const r = normRoot.toLowerCase();
  if (a === r) return ".";
  if (a.startsWith(r + "/")) return normAbs.slice(normRoot.length).replace(/^\//, "");
  return normAbs;
}

function gitHttpPath(
  endpoint: "status" | "branches" | "diff",
  repo: string,
  extra?: Record<string, string>,
): string {
  const params = new URLSearchParams({ repo: repo || "." });
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v != null && v !== "") params.set(k, v);
    }
  }
  return withToken(`/api/git/${endpoint}?${params.toString()}`);
}

async function tryNativeGit<T extends { ok?: boolean }>(
  ipcCall: (() => Promise<T>) | null,
  httpPath: string,
): Promise<T> {
  if (ipcCall) {
    try {
      const res = await ipcCall();
      if (res?.ok) return res;
    } catch {
      // fall through to harness HTTP
    }
  }
  return getJSONSoft<T>(httpPath);
}

/** True when native git write IPC is available (stage/commit/hunk apply). */
export function gitWritesAvailable(): boolean {
  const bridge = getHarnessIpc();
  return !!(bridge?.git?.status && bridge?.git?.stageFile);
}

export const nativeGit = {
  status: (repo: string): Promise<any> => {
    const bridge = getHarnessIpc();
    const ipc = bridge?.git?.status ? () => bridge.git.status(repo) : null;
    return tryNativeGit(ipc, gitHttpPath("status", repo));
  },
  diff: (repo: string, file?: string): Promise<any> => {
    const bridge = getHarnessIpc();
    const ipc = bridge?.git?.diff ? () => bridge.git.diff(repo, file) : null;
    const extra = file ? { file } : undefined;
    return tryNativeGit(ipc, gitHttpPath("diff", repo, extra));
  },
  branches: (repo: string): Promise<any> => {
    const bridge = getHarnessIpc();
    const ipc = bridge?.git?.branches ? () => bridge.git.branches(repo) : null;
    return tryNativeGit(ipc, gitHttpPath("branches", repo));
  },
  stageFile: (repo: string, file: string): Promise<any> => {
    const bridge = getHarnessIpc();
    return bridge?.git?.stageFile
      ? bridge.git.stageFile(repo, file)
      : Promise.resolve({ ok: false, error: "web build" });
  },
  unstageFile: (repo: string, file: string): Promise<any> => {
    const bridge = getHarnessIpc();
    return bridge?.git?.unstageFile
      ? bridge.git.unstageFile(repo, file)
      : Promise.resolve({ ok: false, error: "web build" });
  },
  stageAll: (repo: string): Promise<any> => {
    const bridge = getHarnessIpc();
    return bridge?.git?.stageAll
      ? bridge.git.stageAll(repo)
      : Promise.resolve({ ok: false, error: "web build" });
  },
  unstageAll: (repo: string): Promise<any> => {
    const bridge = getHarnessIpc();
    return bridge?.git?.unstageAll
      ? bridge.git.unstageAll(repo)
      : Promise.resolve({ ok: false, error: "web build" });
  },
  commit: (repo: string, message: string): Promise<any> => {
    const bridge = getHarnessIpc();
    return bridge?.git?.commit
      ? bridge.git.commit(repo, message)
      : Promise.resolve({ ok: false, error: "web build" });
  },
  diffStaged: (repo: string, file?: string): Promise<any> => {
    const bridge = getHarnessIpc();
    const ipc = bridge?.git?.diffStaged ? () => bridge.git.diffStaged(repo, file) : null;
    const extra: Record<string, string> = { staged: "1" };
    if (file) extra.file = file;
    return tryNativeGit(ipc, gitHttpPath("diff", repo, extra));
  },
  applyHunk: (repo: string, patchText: string, reverse?: boolean): Promise<any> => {
    const bridge = getHarnessIpc();
    return bridge?.git?.applyHunk
      ? bridge.git.applyHunk(repo, patchText, reverse)
      : Promise.resolve({ ok: false, error: "web build" });
  },
};

/** True when the Electron preload bridge is available (live check, not import-time). */
export function isDesktop(): boolean {
  return !!getHarnessIpc();
}

// Native folder picker. Electron: OS dialog via IPC. Web: prompt fallback.
export async function pickFolder(): Promise<string | null> {
  const bridge = getHarnessIpc();
  if (bridge && typeof bridge.pickFolder === "function") {
    try { return await bridge.pickFolder(); } catch { return null; }
  }
  const p = (typeof window !== "undefined") ? window.prompt("Absolute path to folder:") : null;
  return p && p.trim() ? p.trim() : null;
}
