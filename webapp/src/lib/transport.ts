// Transport abstraction -- the seam that keeps pm-harness NOT web-locked.
//
// Every backend interaction goes through this module. Today it uses fetch + SSE
// against the local Python harness server. When we package as an Electron app,
// ONLY this file changes: getJSON/postJSON/stream route through window.harnessIPC
// (preload bridge) instead of HTTP. Components never know the difference.

import { EndpointSessionClient, endpointRecoveryError, isEndpointMismatch } from "./endpointSession";

import {
  DESKTOP_BRIDGE_MISSING,
  desktopBridgeMissing,
  desktopBridgeMissingDiagnostic,
} from "./operationalDiagnostic";
import { publishDiagnostic } from "./operationalDiagnosticBus";
import { correlationHeaders, setCorrelationId } from "./correlationId";
import { parseJSONResponse, type JSONResponse } from "../../electron/json-response.mjs";
import { publishTransportFailure, type TransportFailureContext } from "./transportFailure";

/**
 * Live SSE payload from /api/chat, /api/auto, /api/run.
 * Chat/auto omit `turn` (ConvEvent); classic /run includes it (SessionEvent).
 * Keep both shapes — do not require `turn` for chat consumers.
 */
export type StreamEvent = {
  kind: string;
  data?: any;
  turn?: number;
  /** Present on live chatEvents ``?watch=1`` ring frames (not classic /api/chat). */
  cursor?: number;
};

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

/** One store event from GET /api/session/events (unified cursor). */
export type StoreEvent = {
  id: number;
  kind: "stream" | "runners" | "ring_miss" | string;
  data: any;
  session_id?: string;
};

/** Payload for GET /api/session/events (read_events_since). */
export type StoreEventsSince = {
  replay_reset?: boolean;
  stream_id?: string;
  ok?: boolean;
  session_id: string;
  cursor: number;
  events: StoreEvent[];
  gap?: boolean;
};

/** Build the tokened URL for chat event replay / reattach. */
export function chatEventsPath(opts: {
  session?: string;
  since?: number;
  generation?: number;
  /** Live SSE ring watch (mid-turn reattach); omit for JSON pull replay. */
  watch?: boolean;
} = {}): string {
  const params = new URLSearchParams();
  if (opts.session) params.set("session", opts.session);
  if (opts.since != null) params.set("since", String(opts.since));
  if (opts.generation != null) params.set("generation", String(opts.generation));
  if (opts.watch) params.set("watch", "1");
  const q = params.toString();
  return withToken(`/api/chat/events${q ? `?${q}` : ""}`);
}

/** Build the tokened URL for the unified session store event cursor. */
export function sessionEventsPath(opts: {
  session?: string;
  since?: number;
  generation?: number;
} = {}): string {
  const params = new URLSearchParams();
  if (opts.session) params.set("session", opts.session);
  if (opts.since != null) params.set("since", String(opts.since));
  if (opts.generation != null) params.set("generation", String(opts.generation));
  const q = params.toString();
  return withToken(`/api/session/events${q ? `?${q}` : ""}`);
}

/** Live Electron preload bridge (do not freeze at module import). */
export function getHarnessIpc(): any {
  if (typeof window === "undefined") return null;
  return (window as any).harnessIPC || null;
}

/** Desktop shell without harnessIPC — one root diagnostic, do not fetch. */
function refuseIfDesktopBridgeMissing(operation: string, path?: string): void {
  if (!desktopBridgeMissing()) return;
  const diag = desktopBridgeMissingDiagnostic({ operation });
  publishDiagnostic(diag);
  const err = new Error(diag.summary) as Error & { diagnostic?: typeof diag; code?: string; path?: string };
  err.diagnostic = diag;
  err.code = DESKTOP_BRIDGE_MISSING;
  if (path) err.path = path;
  throw err;
}

// Web/vite builds may set window.__HARNESS_TOKEN__ for headered fetch.
// Desktop Electron does not inject the token into the renderer — API calls
// go through harnessIPC (main attaches X-Harness-Token) or webRequest
// interception for loopback /api/ fetches.
function authToken(): string {
  if (typeof window === "undefined") return "";
  const w = window as any;
  if (w.__HARNESS_TOKEN__) return w.__HARNESS_TOKEN__;
  return "";
}

function requestHeaders(extra?: Record<string, string>): Record<string, string> {
  return {
    "X-Harness-Token": authToken(),
    ...correlationHeaders(),
    ...extra,
  };
}

function noteResponseCorrelation(response: Response): void {
  const headers = response && response.headers;
  if (!headers || typeof headers.get !== "function") return;
  const cid = headers.get("X-Correlation-Id");
  if (cid) setCorrelationId(cid);
}

export function withToken(path: string): string {
  // Auth is supplied via the `X-Harness-Token` header; we never append auth
  // tokens to URLs.
  return path;
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

export type JSONRequestContext = Pick<TransportFailureContext, "sessionId" | "repo" | "failureKind"> & { operation?: string };

const WORKTREE_ACTION_PATHS = new Set([
  "/api/worktrees/add", "/api/worktrees/remove", "/api/worktrees/prune",
  "/api/worktrees/prune-edit-branches", "/api/worktrees/max",
]);

function requestContext(method: "GET" | "POST", path: string, body: unknown, context: JSONRequestContext): JSONRequestContext {
  const url = new URL(path, "http://harness.local");
  const fields = body && typeof body === "object" ? body : {};
  const sessionId = "session_id" in fields && typeof fields.session_id === "string" ? fields.session_id
    : "session" in fields && typeof fields.session === "string" ? fields.session : undefined;
  const repo = "repo" in fields && typeof fields.repo === "string" ? fields.repo : undefined;
  return {
    sessionId: context.sessionId ?? (sessionId || url.searchParams.get("session") || url.searchParams.get("session_id") || undefined),
    repo: context.repo ?? (repo || url.searchParams.get("repo") || undefined),
    failureKind: context.failureKind ?? (method === "POST" && WORKTREE_ACTION_PATHS.has(url.pathname) ? "action" : "operational"),
  };
}

const endpointClient = new EndpointSessionClient();

async function rawJSON(method: "GET" | "POST", path: string, body: unknown, identity: Record<string,string>): Promise<JSONResponse> {
  const bridge = getHarnessIpc();
  if (bridge) {
    if (!bridge.requestJSON || bridge.endpointHeaders !== true) {
      throw new Error("Desktop connection bridge needs an update. Quit and reopen Marionette.");
    }
    return bridge.requestJSON(method, path, body, correlationHeaders()["X-Correlation-Id"], identity);
  }
  const response = await fetch(path, {
    method,
    redirect: "error",
    headers: requestHeaders({...identity, ...(method === "POST" ? {"Content-Type":"application/json"} : {})}),
    ...(method === "POST" ? {body: JSON.stringify(body)} : {}),
  });
  return {kind:"response", status:response.status, text:await response.text(), correlationId:response.headers.get("X-Correlation-Id") || ""};
}

function discoverEndpoint(): Promise<JSONResponse> {
  return rawJSON("GET", "/api/endpoint", undefined, {"X-Harness-Protocol":"1"});
}

async function requestJSON<T>(method: "GET" | "POST", path: string, body: unknown, soft: boolean, context: JSONRequestContext): Promise<T> {
  const operation = method === "POST" ? "postJSON" : soft ? "getJSONSoft" : "getJSON";
  const ctx = { operation: context.operation ?? operation, path, ...requestContext(method, path, body, context) };
  refuseIfDesktopBridgeMissing(operation, path);
  try {
    const pin = await endpointClient.connect(discoverEndpoint);
    const request = endpointClient.prepare(path, pin);
    let response: JSONResponse;
    try {
      response = await rawJSON(method, request.path, body, endpointClient.headers(pin));
    } catch (error) {
      endpointClient.invalidate(pin);
      throw error;
    }
    if (response.kind === "connection-error") endpointClient.invalidate(pin);
    if (isEndpointMismatch(response)) {
      endpointClient.invalidate(pin);
      await endpointClient.connect(discoverEndpoint);
      throw endpointRecoveryError();
    }
    if (response.kind === "connection-error") parseJSONResponse(response, path);
    if (!endpointClient.isCurrent(pin)) throw endpointRecoveryError();
    if (request.session && response.kind === "response" && response.status === 409) {
      endpointClient.resetReplay(request);
    }
    if (response.kind === "response" && response.correlationId) setCorrelationId(response.correlationId);
    const parsed = parseJSONResponse(response, path, soft);
    if (soft && response.kind === "response" && (response.status < 200 || response.status >= 300)) {
      try { parseJSONResponse(response, path); } catch (err) {
        publishTransportFailure(err, ctx);
      }
    }
    // API callers supply their endpoint schema; JSON transport cannot validate it.
    return (response.kind === "response" && response.status >= 200 && response.status < 300
      ? endpointClient.accept(request, parsed) : parsed) as T;
  } catch (err) {
    publishTransportFailure(err, ctx);
    throw err;
  }
}

/** Image locators contain no credentials; only this transport may fetch them. */
export function imagePath(path: string): string {
  return "/api/image?path=" + encodeURIComponent(path);
}

function imageOrigin(): string {
  if (!getHarnessIpc()) return window.location.origin;
  const port = "__HARNESS_PORT__" in window ? Number(window.__HARNESS_PORT__) : NaN;
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Backend image port unavailable");
  return `http://127.0.0.1:${port}`;
}

/** Validate before attaching credentials, including before endpoint discovery. */
function imageRequestPath(src: string, origin: string): string {
  const url = new URL(src, origin);
  if (!/^https?:$/.test(url.protocol) || url.origin !== origin || url.pathname !== "/api/image"
    || url.username || url.password || url.hash || !url.searchParams.get("path")
    || [...url.searchParams.keys()].some(key => key !== "path" && key !== "_r")
    || url.searchParams.getAll("path").length !== 1) {
    throw new Error("Refusing image URL outside the backend image endpoint");
  }
  return url.pathname + url.search;
}

export const MAX_IMAGE_BYTES = 32 * 1024 * 1024;

function nativeImage(path: string, identity: Record<string, string>, port: number,
  bridge: {requestImage: (path: string, identity: Record<string, string>, port: number, done: (value: unknown) => void) => () => void},
  signal: AbortSignal): Promise<unknown> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let cancel: (() => void) | undefined;
    const abort = () => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      cancel?.();
      reject(new DOMException("Image load aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, {once: true});
    if (signal.aborted) { abort(); return; }
    try {
      cancel = bridge.requestImage(path, identity, port, value => {
        if (settled) return;
        settled = true;
        signal.removeEventListener("abort", abort);
        resolve(value);
      });
      if (signal.aborted) cancel();
    } catch {
      signal.removeEventListener("abort", abort);
      settled = true;
      reject(new Error("Native image request failed"));
    }
  });
}

function nativeImageBlob(value: unknown, port: number, invalidate: () => void): Blob {
  if (!value || typeof value !== "object" || !("kind" in value)) throw new Error("Invalid native image response");
  if (value.kind === "image-error") {
    if ("code" in value && (value.code === "stale" || value.code === "connection")) invalidate();
    throw new Error("Native image request failed");
  }
  if (value.kind !== "image-response" || !("status" in value) || typeof value.status !== "number"
    || !Number.isInteger(value.status) || !("port" in value) || value.port !== port) throw new Error("Invalid native image response");
  if (value.status === 409) { invalidate(); throw endpointRecoveryError(); }
  if (value.status < 200 || value.status >= 300) throw new Error(`Image request failed (${value.status})`);
  if (!("mime" in value) || typeof value.mime !== "string" || !/^image\/[a-z0-9.+-]+$/.test(value.mime)
    || !("bytes" in value)) throw new Error("Invalid native image response");
  const bytes = value.bytes;
  if (!(bytes instanceof ArrayBuffer) && !(bytes instanceof Uint8Array)) throw new Error("Invalid native image bytes");
  if (!bytes.byteLength || bytes.byteLength > MAX_IMAGE_BYTES) throw new Error("Invalid native image size");
  return new Blob([new Uint8Array(bytes)], {type: value.mime});
}

export async function fetchImage(src: string, signal: AbortSignal): Promise<{blob: Blob; assertCurrent: () => void}> {
  refuseIfDesktopBridgeMissing("fetchImage");
  const origin = imageOrigin();
  const path = imageRequestPath(src, origin);
  const bridge = getHarnessIpc();
  const token = authToken();
  signal.throwIfAborted();
  const pin = await new Promise<Awaited<ReturnType<EndpointSessionClient["connect"]>>>((resolve, reject) => {
    const abort = () => reject(new DOMException("Image load aborted", "AbortError"));
    signal.addEventListener("abort", abort, {once: true});
    endpointClient.connect(discoverEndpoint).then(value => {
      signal.removeEventListener("abort", abort);
      resolve(value);
    }, error => {
      signal.removeEventListener("abort", abort);
      reject(error);
    });
  });
  const current = () => {
    signal.throwIfAborted();
    if (!endpointClient.isCurrent(pin) || imageOrigin() !== origin
      || getHarnessIpc() !== bridge || authToken() !== token) throw endpointRecoveryError();
  };
  current();
  const request = endpointClient.prepare(path, pin);
  if (bridge) {
    if (typeof bridge.requestImage !== "function") throw new Error("Desktop image bridge needs an update. Quit and reopen Marionette.");
    let value: unknown;
    try {
      value = await nativeImage(request.path, endpointClient.headers(pin), Number(new URL(origin).port), bridge, signal);
    } catch (error) {
      current();
      endpointClient.invalidate(pin);
      throw error;
    }
    current();
    const blob = nativeImageBlob(value, Number(new URL(origin).port), () => endpointClient.invalidate(pin));
    current();
    return {blob, assertCurrent: current};
  }
  let response: Response;
  try {
    response = await fetch(request.path, {
      headers: requestHeaders(endpointClient.headers(pin)),
      signal, cache: "no-store", credentials: "omit", redirect: "error",
    });
  } catch (error) {
    if (!signal.aborted) endpointClient.invalidate(pin);
    throw error;
  }
  try {
    current();
    if (response.status === 409) {
      endpointClient.invalidate(pin);
      throw endpointRecoveryError();
    }
    if (!response.ok) throw new Error(`Image request failed (${response.status})`);
    const mime = (response.headers.get("Content-Type") || "").split(";", 1)[0].trim().toLowerCase();
    if (!/^image\/[a-z0-9.+-]+$/.test(mime)) throw new Error("Backend returned a non-image response");
    const length = Number(response.headers.get("Content-Length"));
    if (length > MAX_IMAGE_BYTES) throw new Error("Image exceeds 32 MiB limit");
    if (!response.body) throw new Error("Image response has no body");
    const reader = response.body.getReader();
    const abortBody = () => { void reader.cancel().catch(() => {}); };
    signal.addEventListener("abort", abortBody, {once: true});
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    let size = 0;
    try {
      while (true) {
        const {done, value} = await reader.read();
        current();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_IMAGE_BYTES) throw new Error("Image exceeds 32 MiB limit");
        chunks.push(new Uint8Array(value));
      }
    } finally {
      signal.removeEventListener("abort", abortBody);
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
    current();
    if (!size) throw new Error("Image response is empty");
    return {blob: new Blob(chunks, {type: mime}), assertCurrent: current};
  } finally {
    if (!response.body?.locked) void response.body?.cancel().catch(() => {});
  }
}

/** Like getJSON but returns parsed JSON for non-2xx responses instead of throwing. */
export function getJSONSoft<T = any>(path: string, context: JSONRequestContext = {}): Promise<T> {
  return requestJSON<T>("GET", path, undefined, true, context);
}

export function getJSON<T = any>(path: string, context: JSONRequestContext = {}): Promise<T> {
  return requestJSON<T>("GET", path, undefined, false, context);
}

export function postJSON<T = any>(path: string, body: unknown, context: JSONRequestContext = {}): Promise<T> {
  return requestJSON<T>("POST", path, body, false, context);
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
  onError?: (e: any) => void,
): () => void {
  const ctx = {operation:"stream", path, ...requestContext("GET", path, undefined, {})};
  let cancelled = false;
  let cancel: (() => void) | undefined;
  void (async () => {
    try {
      refuseIfDesktopBridgeMissing("stream", path);
      const pin = await endpointClient.connect(discoverEndpoint);
      if (cancelled) return;
      const fail = (error: unknown) => {
        if (cancelled) return;
        cancelled = true;
        cancel?.();
        // Stream errors are sanitized by Electron, so any 409 forces discovery.
        if ((error && typeof error === "object" && "status" in error && error.status === 409) || isTransientHarnessConnError(error)) {
          endpointClient.invalidate(pin);
          void endpointClient.connect(discoverEndpoint).catch(() => {});
        }
        publishTransportFailure(error, ctx);
        onError?.(error);
      };
      const request = endpointClient.prepare(path, pin);
      if (request.ringReset) onEvent({kind:"endpoint_replay_reset"});
      if (cancelled) return;
      cancel = streamConnected(request.path, ev => {
        if (cancelled) return;
        if (!endpointClient.isCurrent(pin)) { fail(endpointRecoveryError()); return; }
        onEvent(ev);
      }, () => {
        if (cancelled) return;
        if (!endpointClient.isCurrent(pin)) { fail(endpointRecoveryError()); return; }
        onDone?.();
      }, fail, endpointClient.headers(pin));
    } catch (error) {
      if (!cancelled) {
        publishTransportFailure(error, ctx);
        onError?.(error);
      }
    }
  })();
  return () => { cancelled = true; cancel?.(); };
}

function streamConnected(
  path: string,
  onEvent: (ev: StreamEvent) => void,
  onDone: (() => void) | undefined,
  onError: ((e: any) => void) | undefined,
  identity: Record<string,string>
): () => void {
  try {
    refuseIfDesktopBridgeMissing("stream", path);
  } catch (err) {
    onError?.(err);
    return () => {};
  }
  const bridge = getHarnessIpc();
  if (bridge?.stream) return bridge.stream(path, onEvent, onDone, onError, identity);

  // Browser: SSE via fetch so we can attach the auth header (EventSource
  // cannot set custom headers).
  // Mirror electron/stream-bridge.cjs: exactly one terminal callback. Body end
  // without a framing {kind:"done"} must still invoke onDone so Conversation
  // can abort/settle chrome instead of sticking busy forever.
  const controller = new AbortController();
  (async () => {
    let resp: Response | null = null;
    let settled = false;
    const finishDone = () => {
      if (settled) return;
      settled = true;
      onDone?.();
    };
    const finishError = (e: unknown) => {
      if (settled) return;
      settled = true;
      onError?.(e);
    };
    try {
      resp = await fetch(path, {
        method: "GET",
        headers: requestHeaders(identity),
        signal: controller.signal,
      });
      if (!resp.ok) {
        parseJSONResponse({kind:"response",status:resp.status,text:await resp.text(),correlationId:""}, path);
      }
      noteResponseCorrelation(resp);
      const body = resp.body;
      if (!body) throw new Error(`stream ${path}: missing response body`);

      const reader = body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = frame.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const payload = line.slice(6);
          let ev: StreamEvent;
          try { ev = JSON.parse(payload); } catch { continue; }
          if (ev.kind === "done") {
            finishDone();
            controller.abort();
            return;
          }
          if (!settled) onEvent(ev);
        }
      }
      // Natural body EOF (no framing done): settle via onDone, not silent hang.
      if (!controller.signal.aborted) finishDone();
    } catch (e: any) {
      if (!controller.signal.aborted) finishError(e);
    } finally {
      // Reader may still lock the body; cancel() rejects async — swallow it.
      void Promise.resolve(resp?.body?.cancel()).catch(() => {});
    }
  })();

  return () => controller.abort();
}

// Upload a file (multipart). In Electron a browser File object cannot cross the
// IPC boundary, so we read it into bytes and hand {name, type, bytes} to the main
// process, which POSTs a multipart body to the loopback backend. On the web build
// (real same-origin server) we use a normal multipart fetch.
export async function uploadFile(file: File): Promise<{ path: string; name: string }[]> {
  refuseIfDesktopBridgeMissing("uploadFile");
  const pin = await endpointClient.connect(discoverEndpoint);
  const identity = endpointClient.headers(pin);
  const bridge = getHarnessIpc();
  if (bridge?.uploadFile) {
    const buf = await file.arrayBuffer();
    if (!endpointClient.isCurrent(pin)) throw endpointRecoveryError();
    const result = await bridge.uploadFile({ name: file.name, type: file.type, bytes: new Uint8Array(buf) }, identity);
    if (!endpointClient.isCurrent(pin)) throw endpointRecoveryError();
    if (result?.status === 409) {
      endpointClient.invalidate(pin);
      await endpointClient.connect(discoverEndpoint);
      throw endpointRecoveryError();
    }
    if (Array.isArray(result)) return result;
    if (result && typeof result === "object") {
      if (result.error) throw new Error(String(result.error));
      return result.saved || [];
    }
    return [];
  }
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/upload", { method: "POST", body: fd, headers: requestHeaders(identity) });
  if (!endpointClient.isCurrent(pin)) throw endpointRecoveryError();
  if (r.status === 409) {
    endpointClient.invalidate(pin);
    await endpointClient.connect(discoverEndpoint);
    throw endpointRecoveryError();
  }
  const j = await r.json().catch(() => ({}));
  if (!endpointClient.isCurrent(pin)) throw endpointRecoveryError();
  if (!r.ok) {
    throw new Error((j && j.error) || `Upload failed (${r.status})`);
  }
  return j.saved || [];
}

// Native desktop bridges (file tree + git). Web build returns not-supported.
export type NativeOpenResult = { ok: true; action: "opened" | "revealed" } | { ok: false; error: string };

export const nativeFs = {
  openPath: async (target: string): Promise<NativeOpenResult> => {
    const bridge = getHarnessIpc();
    if (typeof bridge?.fs?.openPath !== "function") {
      return { ok: false, error: "Open this link in the updated Marionette desktop app, or open the path in your file manager." };
    }
    try {
      return await bridge.fs.openPath(target);
    } catch (error) {
      return { ok: false, error: String(error) };
    }
  },
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
