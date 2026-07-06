// Typed harness API -- thin wrappers over the transport seam.
import { getJSON, postJSON, stream, withToken, uploadFile, type StreamEvent } from "./transport";

export type Config = {
  driver: string; reach: string; budget: number;
  models?: string[]; preflight?: string | null;
  repo?: string;
  swarm_adapter?: string;
  edit_engine?: "agentic" | "native";
  agentic_ready?: boolean;
};
export type Settings = {
  driver: string;
  reach: string;
  budget: number;
  models: string[];
  auto_distill: boolean;
  reviewEditsBeforeApply?: boolean;
  autoVerify?: boolean;
  autoCommandGuard?: boolean;
  commandTimeout?: string;
  maxPilotSteps?: string;
  wiki_auto?: boolean;
  state_dir: string;
  repo: string;
  has_api_key?: boolean;
  api_key_masked?: string;
  key_env_var?: string;
  preflight_ok?: boolean;
};

export type PendingReviewHunk = {
  id: string;
  header: string;
  lines: string[];
  status: "pending" | "accept" | "reject";
};

export type PendingReviewFile = {
  path: string;
  hunks: PendingReviewHunk[];
};

export type PendingReview = {
  id: string;
  job_id: string;
  objective: string;
  files: PendingReviewFile[];
  created_at: number;
};
export type Task = {
  id: string;
  role: string;
  instruction: string;
  status: string;
  adapter: string;
  completed_at?: string | null;
};
export type Job = {
  id: string;
  goal: string;
  status: string;
  role?: string;
  adapter?: string;
  created_at?: string | null;
  updated_at?: number | string | null;
  tokens?: number;
  est_cost_usd?: number;
  task_count?: number;
  tasks?: Task[];
  // /api/jobs sends an artifact COUNT; embedded views may send the full list.
  // Use the /api/artifacts endpoint to fetch details for a job.
  artifacts?: Artifact[] | number;
};
export type Artifact = {
  id?: string;
  type: string;
  headline: string;
  confidence?: number;
  created_by?: string;
  model?: string;
  est_cost_usd?: number;
  role?: string;
  rejected?: { model: string; reason: string }[];
  detail?: any;
  // Present only for patch artifacts: the list of touched files and a parsed
  // diffstat so job cards can show "3 files +40 -12" instead of truncated text.
  files?: string[] | null;
  diffstat?: { files: number; insertions: number; deletions: number } | null;
};
// Job.artifacts is a count in /api/jobs but a full list in /swarm/live; this
// narrows to the embedded list (empty when the payload only carried a count).
export function jobArtifactList(j: Job): Artifact[] {
  return Array.isArray(j.artifacts) ? j.artifacts : [];
}
export type SwarmLive = {
  session: {
    tokens_used: number;
    est_cost_usd: number;
    driver?: string;
    tool_output_tokens_saved?: number;
    tool_output_savings_usd?: number;
    tool_output_compactions?: number;
  };
  jobs: Job[];
};
export type Workspace = { name: string; branch: string; active: boolean; dirty?: boolean };
export type Session = { id: string; title: string; created: number; active?: boolean; archived?: boolean; repo?: string; branch?: string };

export type SessionState = {
  state: "idle" | "thinking" | "awaiting_swarm";
  pending_swarms: boolean;
  // Set when the transcript ends on an unanswered user turn while idle -- the
  // signal to auto-continue after a backend restart (self-edit apply).
  resume_pending?: boolean;
};

export type SwarmResultData = {
  job_id: string;
  applied: boolean;
  files: string[];
  summary: string;
  error: string | null;
  objective?: string;
};

export type SwarmResultEvent = {
  kind: "swarm_result";
  data: SwarmResultData;
};

export type SwarmResultsResponse = {
  results: SwarmResultEvent[];
};

export type PlatformAdapter = {
  name: string;
  enabled: boolean;
  implement_capable: boolean;
  available: boolean;
  note: string;
};

export type GitStatus = {
  gh_available: boolean;
  gh_user: string | null;
  wiki_repo: string | null;
  connected: boolean;
  html_url: string | null;
};

export type GitConnectResponse = GitStatus & {
  device_code?: string;
  user_code?: string;
  verification_uri?: string;
  interval?: number;
  expires_in?: number;
  error?: string;
};

export type GitPollResponse = GitStatus & {
  status?: string;
  error?: string;
};

export type Worktree = {
  path: string;
  branch: string;
  head: string;
  is_main: boolean;
  locked: boolean;
};

export type Hook = {
  id: string;
  event: string;
  command: string;
  enabled: boolean;
};

export type ProviderInfo = {
  name: string;
  display_name?: string;
  env_var: string;
  base_url: string;
  has_key: boolean;
  masked?: string;
  api_mode: string;
  has_env?: boolean;
  disconnected?: boolean;
};

export type ProviderKeyResult = {
  ok: boolean;
  provider: string;
  has_key: boolean;
  masked: string;
};

export type ProbeModel = {
  id: string;
};

export type ProbeResult = {
  provider: string;
  models: ProbeModel[];
  source: "live" | "static";
  error?: string;
};

export type RegistryModel = {
  id: string;
  adapter: string;
  adapter_model_name?: string;
  capability_score: number;
  tags?: string[];
  input_per_mtok_usd?: number;
  output_per_mtok_usd?: number;
  notes?: string;
};

export type RolesConfig = {
  roles: Record<string, number>;
  policies: string[];
  routing_policy: string;
  overrides: Record<string, number>;
};

export type PilotValidateResult = {
  valid: boolean;
  resolved_model_id: string | null;
  provider: string | null;
  reason: string;
};

export type RecommendResult = {
  pilot: string;
  pilot_driver: string;
  roles: Record<string, string>;
};

export type UsageData = {
  session: {
    tokens_used: number;
    est_cost_usd: number;
    driver: string;
    price_in: number;
    price_out: number;
    tokens_cached?: number;
    cache_savings_usd?: number;
    tool_output_tokens_saved?: number;
    tool_output_savings_usd?: number;
    tool_output_compactions?: number;
  };
  jobs: {
    job_id: string;
    tokens: number;
    est_cost_usd: number;
  }[];
};

export type Checkpoint = {
  id: string;
  label: string;
  trigger: string;
  timestamp: number;
  head: string | null;
};

export type CheckpointDiffFile = {
  path: string;
  status: "modified" | "added" | "removed";
};

export type CheckpointDiff = {
  ok: boolean;
  diff: string;
  files: CheckpointDiffFile[];
  truncated: boolean;
  error?: string;
};

export type ModelCatalogEntry = {
  provider: string;
  provider_display: string;
  model: string;
  spec: string;
  available: boolean;
  enabled: boolean;
};

export type ModelCatalogResponse = {
  catalog: ModelCatalogEntry[];
  all: ModelCatalogEntry[];
  enabled: string[];
};

export type CodegraphStatus = {
  indexed: boolean;
  status: "ready" | "indexing" | "unsupported" | "none";
  reason?: string | null;
  nodes: number | null;
  edges: number | null;
  files: number | null;
  languages: string[] | null;
  last_indexed: string | null;
  repo: string;
};

export type WikiGraphData = {
  configured: boolean;
  status: "ok" | "not_configured" | "error";
  nodes: { id: string; title: string; section?: string; tags?: string[] }[];
  edges: { source: string; target: string }[];
  error?: string;
  base_url?: string;
};

export type ContextCategory = {
  name: string;
  tokens: number;
};

export type ContextUsageResponse = {
  total: number;
  limit: number;
  categories: ContextCategory[];
  tool_output_tokens_saved?: number;
  tool_output_savings_usd?: number;
  tool_output_compactions?: number;
};

// Above this many characters, a chat message / autopilot objective is routed
// through POST /api/chat/stash instead of the SSE GET's query string -- see
// api.chat() for why (URL length limits silently drop large pastes).
const CHAT_STASH_THRESHOLD = 4000;

export const api = {
  providers: () => getJSON<ProviderInfo[]>("/api/providers"),
  probeProvider: (provider: string) => postJSON<ProbeResult>("/api/providers/probe", { provider }),
  setProviderKey: (provider: string, api_key: string) => postJSON<ProviderKeyResult>("/api/providers/key", { provider, api_key }),
  clearProviderKey: (provider: string) => postJSON<ProviderKeyResult>("/api/providers/key", { provider, action: "clear" }),
  setProviderEnabled: (provider: string, enabled: boolean) =>
    postJSON<ProviderKeyResult>("/api/providers/key", { provider, action: enabled ? "enable" : "disable" }),
  getRegistry: () => getJSON<{ models: RegistryModel[] }>("/api/registry"),
  saveRegistry: (models: RegistryModel[]) => postJSON<{ ok: boolean; models: RegistryModel[] }>("/api/registry", { models }),
  getRoles: () => getJSON<RolesConfig>("/api/roles"),
  saveRoles: (payload: { overrides: Record<string, number>; routing_policy?: string }) =>
    postJSON<{ ok: boolean; overrides: Record<string, number>; routing_policy: string }>("/api/roles", payload),
  validatePilot: (driver: string) => postJSON<PilotValidateResult>("/api/pilot/validate", { driver }),
  recommend: () => getJSON<RecommendResult>("/api/registry/recommend"),

  config: () => getJSON<Config>("/api/config"),
  getUsage: () => getJSON<UsageData>("/api/usage"),
  settings: () => getJSON<Settings>("/api/settings"),
  updateSettings: (partial: Partial<Settings> & { api_key?: string; clear_api_key?: boolean }) => postJSON<Settings>("/api/settings", partial),
  jobs: () => getJSON<Job[]>("/api/jobs"),
  swarmLive: () => getJSON<SwarmLive>(withToken("/api/swarm/live")),
  swarmCancel: (jobId: string) =>
    postJSON<{ ok: boolean; job_id?: string; error?: string }>(withToken("/api/swarm/cancel"), { job_id: jobId }),
  artifacts: (jobId: string) => getJSON<Artifact[]>(`/api/artifacts?job_id=${encodeURIComponent(jobId)}`),
  workspaces: () => getJSON<Workspace[]>("/api/workspaces"),
  switchWorkspace: (name: string) => postJSON("/api/workspaces/switch", { name }),
  createWorkspace: (name: string, branch?: string) =>
    postJSON("/api/workspaces/create", { name, branch }),
  sessions: () => getJSON<Session[]>("/api/sessions"),
  sessionTranscript: (session: string) => getJSON<{ history: any[]; display?: any[]; job_ids?: string[] }>(withToken(`/api/sessions/transcript?session=${encodeURIComponent(session)}`)),
  getSessionState: () => getJSON<SessionState>(withToken("/api/session/state")),
  interruptSession: () => postJSON<{ ok: boolean }>("/api/session/interrupt", {}),
  getSwarmResults: () => getJSON<SwarmResultsResponse>(withToken("/api/session/swarm-results")),
  createSession: (title?: string) => postJSON<Session>("/api/sessions/create", { title }),
  switchSession: (id: string) => postJSON("/api/sessions/switch", { id }),
  deleteSession: (id: string) => postJSON<{ ok: boolean; active: string | null }>("/api/sessions/delete", { session: id }),
  archiveSession: (id: string, archived: boolean) => postJSON<{ ok: boolean }>("/api/sessions/archive", { session: id, archived }),
  renameSession: (id: string, title: string) => postJSON<{ ok: boolean }>("/api/sessions/rename", { session: id, title }),
  swapPilot: (model: string) => getJSON(withToken(`/api/pilot?model=${encodeURIComponent(model)}`)),
  uploadImage: async (file: File | Blob): Promise<{ path: string; name: string }> => {
    let fileObj: File;
    if (file instanceof File) {
      fileObj = file;
    } else {
      const ext = file.type.split("/")[1] || "png";
      fileObj = new File([file], `image-${Date.now()}.${ext}`, { type: file.type });
    }
    const saved = await uploadFile(fileObj);
    if (!saved || saved.length === 0) {
      throw new Error("Upload failed");
    }
    return saved[0];
  },
  // Durable src for an uploaded image: the composer's blob: preview URL is
  // revoked right after send, so sent-message thumbnails (and reloaded
  // transcripts) must load the saved file from disk via this tokened GET.
  // An <img> tag loads a raw browser resource -- it does NOT route through the
  // Electron IPC transport the way getJSON/stream do. In the packaged app the
  // renderer is served from file:// (win.loadFile), so a RELATIVE "/api/image"
  // src resolves to file:///api/image and fails -> broken thumbnail. Build an
  // ABSOLUTE backend URL using the port Electron injects as window.__HARNESS_PORT__
  // (present on load and respawn), with the token in the query so the tag can
  // authenticate (an <img> cannot send headers). Falls back to a relative,
  // tokened path for the plain web build (served same-origin from the backend).
  imageUrl: (path: string): string => {
    const rel = withToken("/api/image?path=" + encodeURIComponent(path));
    if (typeof window !== "undefined") {
      const port = (window as any).__HARNESS_PORT__;
      if (port) return `http://127.0.0.1:${port}${rel}`;
    }
    return rel;
  },
  chat: (message: string, onEvent: (e: StreamEvent) => void, onDone?: () => void, onError?: (e: any) => void, plan: boolean = false, images?: string[]) => {
    // The chat stream is an SSE GET (EventSource is GET-only), so the message
    // normally rides in the URL query string. A large paste (e.g. a huge
    // transcript) can push that URL past the HTTP request-line limit and the
    // request gets silently rejected -- the message never reaches the backend
    // and never appears in the chat. Route anything sizeable through a POST
    // "stash" first (see harness /api/chat/stash) and hand the stream only a
    // short id via ?mid= instead. Small messages keep the original, simpler
    // query-param path unchanged.
    const imagesStr = images && images.length > 0 ? images.join("|") : "";
    const needsStash = message.length > CHAT_STASH_THRESHOLD || imagesStr.length > CHAT_STASH_THRESHOLD;
    let cancelled = false;
    let cancelStream: (() => void) | null = null;
    const startStream = (url: string) => {
      if (cancelled) return;
      cancelStream = stream(url, onEvent, onDone, onError);
    };
    if (needsStash) {
      postJSON<{ id: string }>("/api/chat/stash", { message, images: images || [] })
        .then((res) => {
          startStream(`/api/chat?mid=${encodeURIComponent(res.id)}${plan ? "&plan=true" : ""}`);
        })
        .catch((e) => onError?.(e));
    } else {
      let url = `/api/chat?message=${encodeURIComponent(message)}${plan ? "&plan=true" : ""}`;
      if (imagesStr) {
        url += `&images=${encodeURIComponent(imagesStr)}`;
      }
      startStream(url);
    }
    return () => {
      cancelled = true;
      cancelStream?.();
    };
  },
  // Keep-alive continuation: generate a pilot turn off existing history (no new
  // user message). Fired when a background swarm finishes so the pilot assesses
  // the result and continues on its own -- even without autopilot.
  resume: (onEvent: (e: StreamEvent) => void, onDone?: () => void, onError?: (e: any) => void) =>
    stream("/api/chat?resume=true", onEvent, onDone, onError),
  mcp: () => getJSON<{ servers: any[]; tools: any[] }>("/api/mcp"),
  mcpCatalog: () => getJSON<{ catalog: Record<string, any> }>("/api/mcp/catalog"),
  mcpAdd: (name: string, command?: string, args?: string[], env?: Record<string, string>, url?: string) => {
    const payload = url ? { name, url } : { name, command, args, env };
    return postJSON<{ ok: boolean; tools?: number; error?: string }>("/api/mcp/add", payload);
  },
  mcpRemove: (name: string) => postJSON<{ ok: boolean }>("/api/mcp/remove", { name }),
  mcpStart: (name: string) => postJSON<{ ok: boolean; tools?: number; error?: string }>("/api/mcp/start", { name }),
  mcpStop: (name: string) => postJSON<{ ok: boolean }>("/api/mcp/stop", { name }),
  skills: () => getJSON<any[]>("/api/skills"),
  skillDistill: () => postJSON<{ skill?: any; rules?: any }>("/api/skills/distill", {}),
  wikiIngestPrepared: (pages: any[]) => postJSON<{ ok: boolean; ingested: number }>("/api/wiki/ingest-prepared", { pages }),
  modelCatalog: () => getJSON<ModelCatalogResponse>("/api/models/catalog"),
  toggleModel: (spec: string, enabled: boolean) => postJSON<{ ok: boolean; enabled: string[] }>("/api/models/toggle", { spec, enabled }),
  setEnabledModels: (enabled: string[]) => postJSON<{ ok: boolean; enabled: string[] }>("/api/models/set", { enabled }),
  rules: () => getJSON<any[]>("/api/rules"),
  ruleApprove: (slug: string) => postJSON<{ ok: boolean }>("/api/rules/approve", { slug }),
  ruleReject: (slug: string) => postJSON<{ ok: boolean }>("/api/rules/reject", { slug }),
  skillApprove: (slug: string) => postJSON<{ ok: boolean }>("/api/skills/approve", { slug }),
  skillReject: (slug: string) => postJSON<{ ok: boolean }>("/api/skills/reject", { slug }),
  memory: () => getJSON<{ memory: { id: string; text: string; category: string; created_at: number; source: string }[]; total_chars: number; limit: number }>("/api/memory"),
  memoryAdd: (text: string, category?: string) => postJSON<{ id: string; text: string; category: string }>("/api/memory/add", { text, category }),
  memoryRemove: (id: string) => postJSON<{ ok: boolean }>("/api/memory/remove", { id }),
  auto: (objective: string, onEvent: (e: StreamEvent) => void, onDone?: () => void, onError?: (e: any) => void) => {
    // Same URL-length hazard as chat() (autopilot objective can be a large
    // pasted brief) -- route big ones through the stash, small ones inline.
    if (objective.length > CHAT_STASH_THRESHOLD) {
      let cancelled = false;
      let cancelStream: (() => void) | null = null;
      postJSON<{ id: string }>("/api/chat/stash", { message: objective })
        .then((res) => {
          if (cancelled) return;
          cancelStream = stream(`/api/auto?mid=${encodeURIComponent(res.id)}`, onEvent, onDone, onError);
        })
        .catch((e) => onError?.(e));
      return () => {
        cancelled = true;
        cancelStream?.();
      };
    }
    return stream(`/api/auto?objective=${encodeURIComponent(objective)}`, onEvent, onDone, onError);
  },
  exportUrl: (sessionId: string, format: "md" | "json") =>
    withToken(`/api/sessions/export?session=${encodeURIComponent(sessionId)}&format=${format}`),

  getWorktrees: () => getJSON<{ worktrees: Worktree[]; max: number }>("/api/worktrees"),
  addWorktree: (branch: string, base?: string) => postJSON<Worktree>("/api/worktrees/add", { branch, base }),
  removeWorktree: (path: string, force?: boolean) => postJSON<{ ok: boolean }>("/api/worktrees/remove", { path, force }),
  pruneWorktrees: () => postJSON<{ ok: boolean }>("/api/worktrees/prune", {}),
  setWorktreeMax: (max: number) => postJSON<{ ok: boolean }>("/api/worktrees/max", { max }),

  openWorkspace: (path: string) => postJSON<{ ok: boolean; repo: string; branch: string; is_git: boolean; codegraph: "indexing" | "ready" | "unsupported"; active_session?: string }>("/api/workspace/open", { path }),
  forgetWorkspace: (path: string) => postJSON<{ ok: boolean; recents: string[] }>("/api/workspace/forget", { path }),
  getWorkspace: () => getJSON<{ repo: string; branch: string; is_git: boolean; codegraph_status: string; recents?: string[] }>("/api/workspace"),
  getWorkspaceFiles: () => getJSON<{ files: string[] }>(withToken("/api/workspace/files")),
  searchSymbols: (q: string) => getJSON<{ symbols: { name: string; kind: string; path: string; line: number }[]; status?: string }>(withToken("/api/workspace/symbols?q=" + encodeURIComponent(q))),
  readFile: (path: string) => getJSON<{ ok: boolean; path: string; content: string; truncated: boolean; error?: string; binary?: boolean }>("/api/file/read?path=" + encodeURIComponent(path)),
  writeFile: (path: string, content: string) => postJSON<{ ok: boolean; bytes?: number; error?: string }>("/api/file/write", { path, content }),
  inlineEdit: (path: string, selection: string, instruction: string, prefix: string, suffix: string, language: string) => postJSON<{ ok: boolean; edit?: string; error?: string }>("/api/inline-edit", { path, selection, instruction, prefix, suffix, language }),
  compactSession: () => postJSON<{ ok: boolean; before_tokens: number; after_tokens: number }>("/api/session/compact", {}),
  steerSession: (text: string, images?: string[]) =>
    postJSON<{ ok: boolean }>("/api/session/steer", { text, images: images && images.length ? images : undefined }),
  // PROMPT QUEUE: a "playlist" of full user prompts that each run as their own
  // complete turn one after the previous fully finishes. Distinct from steer
  // (a mid-turn interrupt on the CURRENT running turn). Items can be edited /
  // removed / reordered before they run.
  queueList: () => getJSON<{ items: { id: string; text: string; images?: string[] }[] }>(withToken("/api/session/queue")),
  queueAdd: (text: string, images?: string[]) => postJSON<{ ok: boolean; item: { id: string; text: string; images?: string[] } }>("/api/session/queue", { text, images: images || [] }),
  queueRemove: (id: string) => postJSON<{ ok: boolean; id: string }>("/api/session/queue", { id }),
  queueReorder: (ids: string[]) => postJSON<{ ok: boolean; items: { id: string; text: string; images?: string[] }[] }>("/api/session/queue/reorder", { ids }),
  queueClear: () => postJSON<{ ok: boolean; cleared: number }>("/api/session/queue", { clear: true }),
  getContextUsage: () => getJSON<ContextUsageResponse>(withToken("/api/context/usage")),

  getCheckpoints: () => getJSON<Checkpoint[]>(withToken("/api/checkpoints")),
  restoreCheckpoint: (id: string) => postJSON<{ ok: boolean; restored_files: string[]; auto_snapshot_id: string }>("/api/checkpoints/restore", { id }),
  snapshotCheckpoint: (label: string) => postJSON<{ ok: boolean; id: string }>("/api/checkpoints/snapshot", { label }),
  getCheckpointDiff: (id: string) => getJSON<CheckpointDiff>(withToken(`/api/checkpoints/diff?id=${encodeURIComponent(id)}`)),

  getHooks: () => getJSON<{ hooks: Hook[]; events: string[] }>("/api/hooks"),
  addHook: (event: string, command: string) => postJSON<Hook>("/api/hooks/add", { event, command }),
  updateHook: (id: string, patch: { enabled?: boolean; command?: string }) => postJSON<Hook>("/api/hooks/update", { id, ...patch }),
  removeHook: (id: string) => postJSON<{ ok: boolean }>("/api/hooks/remove", { id }),

  getCodegraph: () => getJSON<CodegraphStatus>("/api/codegraph"),
  reindexCodegraph: () => postJSON<{ ok: boolean; status: string }>("/api/codegraph/reindex", {}),
  getWikiGraph: () => getJSON<WikiGraphData>("/api/wiki/graph"),
  getWikiConfig: () => getJSON<{ api_base: string; has_token: boolean }>("/api/wiki/config"),
  setWikiConfig: (api_base?: string, owner_token?: string) =>
    postJSON<{ api_base: string; has_token: boolean }>("/api/wiki/config", { api_base, owner_token }),

  getPlatform: () => getJSON<{ adapters: PlatformAdapter[] }>("/api/platform"),
  togglePlatform: (name: string, enabled: boolean) => postJSON<{ adapters: PlatformAdapter[] }>("/api/platform", { name, enabled }),

  listCommands: () => getJSON<{ commands: { name: string; description: string; scope: string }[] }>("/api/commands"),
  renderCommand: (name: string, args: string) => postJSON<{ name: string; prompt: string }>("/api/commands/render", { name, args }),

  getGitStatus: () => getJSON<GitStatus>(withToken("/api/git/status")),
  connectGit: (method: "gh" | "device") => postJSON<GitConnectResponse>("/api/git/connect", { method }),
  pollGitDevice: (device_code: string) => postJSON<GitPollResponse>("/api/git/device/poll", { device_code }),
  disconnectGit: () => postJSON<GitStatus>("/api/git/disconnect", {}),

  getReviews: () => getJSON<PendingReview[]>(withToken("/api/reviews")),
  applyReview: (id: string, decisions: Record<string, "accept" | "reject">) =>
    postJSON<{ ok: boolean; applied_files: string[]; rejected_hunks: string[]; checkpoint_id: string | null; message: string }>("/api/reviews/apply", { id, decisions }),
  dismissReview: (id: string) => postJSON<{ ok: boolean }>("/api/reviews/dismiss", { id }),
};
