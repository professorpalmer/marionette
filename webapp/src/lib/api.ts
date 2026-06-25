// Typed harness API -- thin wrappers over the transport seam.
import { getJSON, postJSON, stream, type StreamEvent } from "./transport";

export type Config = {
  driver: string; reach: string; budget: number;
  models?: string[]; preflight?: string | null;
  repo?: string;
};
export type Job = { id: string; goal: string; status: string };
export type Artifact = { type: string; headline: string; confidence?: number };
export type Workspace = { name: string; branch: string; active: boolean; dirty?: boolean };
export type Session = { id: string; title: string; created: number; active?: boolean };

export const api = {
  config: () => getJSON<Config>("/api/config"),
  jobs: () => getJSON<Job[]>("/api/jobs"),
  artifacts: (jobId: string) => getJSON<Artifact[]>(`/api/artifacts?job_id=${encodeURIComponent(jobId)}`),
  workspaces: () => getJSON<Workspace[]>("/api/workspaces"),
  switchWorkspace: (name: string) => postJSON("/api/workspaces/switch", { name }),
  createWorkspace: (name: string, branch?: string) =>
    postJSON("/api/workspaces/create", { name, branch }),
  sessions: () => getJSON<Session[]>("/api/sessions"),
  createSession: (title?: string) => postJSON<Session>("/api/sessions/create", { title }),
  switchSession: (id: string) => postJSON("/api/sessions/switch", { id }),
  swapPilot: (model: string) => getJSON(`/api/pilot?model=${encodeURIComponent(model)}`),
  chat: (message: string, onEvent: (e: StreamEvent) => void, onDone?: () => void, onError?: (e: any) => void) =>
    stream(`/api/chat?message=${encodeURIComponent(message)}`, onEvent, onDone, onError),
  auto: (objective: string, onEvent: (e: StreamEvent) => void, onDone?: () => void, onError?: (e: any) => void) =>
    stream(`/api/auto?objective=${encodeURIComponent(objective)}`, onEvent, onDone, onError),
};
