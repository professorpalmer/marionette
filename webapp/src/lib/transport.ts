// Transport abstraction -- the seam that keeps pm-harness NOT web-locked.
//
// Every backend interaction goes through this module. Today it uses fetch + SSE
// against the local Python harness server. When we package as an Electron app,
// ONLY this file changes: getJSON/postJSON/stream route through window.harnessIPC
// (preload bridge) instead of HTTP. Components never know the difference.

export type StreamEvent = { kind: string; data?: any };

// Detect an Electron preload bridge if present (set in a future desktop build).
const ipc: any = (typeof window !== "undefined" && (window as any).harnessIPC) || null;

export async function getJSON<T = any>(path: string): Promise<T> {
  if (ipc?.getJSON) return ipc.getJSON(path);
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

export async function postJSON<T = any>(path: string, body: any): Promise<T> {
  if (ipc?.postJSON) return ipc.postJSON(path, body);
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

// Stream server-sent events. Returns a cancel() function. In Electron this maps
// to an IPC event channel; on the web it's EventSource.
export function stream(
  path: string,
  onEvent: (ev: StreamEvent) => void,
  onDone?: () => void,
  onError?: (e: any) => void
): () => void {
  if (ipc?.stream) return ipc.stream(path, onEvent, onDone, onError);
  const es = new EventSource(path);
  es.onmessage = (m) => {
    let ev: StreamEvent;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.kind === "done") { es.close(); onDone?.(); return; }
    onEvent(ev);
  };
  es.onerror = (e) => { es.close(); onError?.(e); };
  return () => es.close();
}

// Upload a file (multipart). Electron build swaps to a native file path handoff.
export async function uploadFile(file: File): Promise<{ path: string; name: string }[]> {
  if (ipc?.uploadFile) return ipc.uploadFile(file);
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  const j = await r.json();
  return j.saved || [];
}

export const isDesktop = !!ipc;
