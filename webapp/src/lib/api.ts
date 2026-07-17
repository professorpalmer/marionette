// Typed harness API -- thin wrappers over the transport seam.
import {
  getJSON,
  getJSONSoft,
  postJSON,
  stream,
  withToken,
  uploadFile,
  chatEventsPath,
  type StreamEvent,
  type ChatEventReplay as TransportChatEventReplay,
} from "./transport";
import {
  buildSessionSearchQuery,
  normalizeSessionSearchHits,
  type SessionSearchHit,
} from "./sessionSearch";

export type { ChatEventFrame } from "./transport";
export type { SessionSearchHit } from "./sessionSearch";

/** Mid-turn SSE ring replay payload (miss fields from GET /api/chat/events). */
export type ChatEventReplay = TransportChatEventReplay & {
  missed?: boolean;
  available?: boolean;
  code?: "ring_miss" | "generation_mismatch" | string;
};

export type Config = {
  driver: string; reach: string; budget: number;
  models?: string[]; preflight?: string | null;
  repo?: string;
  swarm_adapter?: string;
  edit_engine?: "agentic" | "native";
  agentic_ready?: boolean;
  reasoning_effort?: ReasoningEffort;
};
export type ReasoningEffort = "none" | "low" | "medium" | "high" | "xhigh" | "max";
export type Settings = {
  driver: string;
  reach: string;
  budget: number;
  models: string[];
  auto_distill: boolean;
  reviewEditsBeforeApply?: boolean;
  autoVerify?: boolean;
  autoCommandGuard?: boolean;
  hash_edit_enabled?: boolean;
  commandTimeout?: string;
  maxPilotSteps?: string;
  workerTokenBudget?: string;
  reasoning_effort?: ReasoningEffort;
  wiki_auto?: boolean;
  state_dir: string;
  repo: string;
  has_api_key?: boolean;
  api_key_masked?: string;
  key_env_var?: string;
  preflight_ok?: boolean;
  bedrock?: BedrockStatus;
};

export type BedrockStatus = {
  configured: boolean;
  has_key: boolean;
  auth_mode?: string;
  masked?: string;
  has_bearer?: boolean;
  has_access_key?: boolean;
  has_session_token?: boolean;
  region?: string;
  aws_region?: string;
  bedrock_region?: string;
  model_id?: string;
  disconnected?: boolean;
};

export type BedrockCredentials = {
  AWS_BEARER_TOKEN_BEDROCK?: string;
  AWS_ACCESS_KEY_ID?: string;
  AWS_SECRET_ACCESS_KEY?: string;
  AWS_SESSION_TOKEN?: string;
  AWS_REGION?: string;
  BEDROCK_REGION?: string;
  BEDROCK_MODEL_ID?: string;
};

export type PoolEntryPublic = {
  id: string;
  label: string;
  auth_type: string;
  source: string;
  priority: number;
  last_status?: string;
  last_error_code?: number | null;
  last_error_message?: string | null;
  request_count?: number;
  masked?: string;
  has_refresh?: boolean;
};

export type AuthPoolPublic = {
  provider: string;
  strategy: string;
  oauth_capable?: boolean;
  entries: PoolEntryPublic[];
};

export type AuthPoolsResponse = {
  pools: AuthPoolPublic[];
  strategies?: string[];
  providers?: string[];
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
  /** Measured/estimated tokens for this worker (from usage arts). */
  tokens?: number;
  /** Usage-priced cost, or routing estimate while usage is absent. */
  est_cost_usd?: number;
};
export type Job = {
  id: string;
  goal: string;
  status: string;
  role?: string;
  adapter?: string;
  /** "harness" (Marionette-dispatched) or "cli" (Cursor MCP / terminal PM). */
  source?: "harness" | "cli" | string;
  created_at?: string | null;
  updated_at?: number | string | null;
  tokens?: number;
  est_cost_usd?: number;
  model?: string;
  /** Prompt-cache hits on this job's usage-bearing artifacts. */
  tokens_cached?: number;
  /** Router baseline-vs-chosen savings for this job (balanced/cheap). */
  routing_saved_usd?: number;
  /** Swarm prompt-cache savings priced from this job's usage x registry. */
  cache_saved_usd?: number;
  tool_output_tokens_saved?: number;
  tool_output_savings_usd?: number;
  tool_output_compactions?: number;
  task_count?: number;
  tasks?: Task[];
  // /api/jobs sends an artifact COUNT; embedded views may send the full list.
  // Use the /api/artifacts endpoint to fetch details for a job.
  artifacts?: Artifact[] | number;
  // False on /api/swarm/live (slim routing+verdicts for in-progress and terminal).
  // Expand fetches /api/artifacts and flips this true.
  artifacts_complete?: boolean;
  // Server-computed before slim: all workers failed/blocked with no real work.
  dead_run_failure?: string | null;
};
export type Artifact = {
  id?: string;
  type: string;
  headline: string;
  confidence?: number;
  created_by?: string;
  // Present on ROUTING (and some worker) artifacts so the GUI can group
  // router + router-fallback pairs into one display row per task.
  task_id?: string;
  model?: string;
  est_cost_usd?: number;
  role?: string;
  rejected?: { model: string; reason: string }[];
  detail?: any;
  // Present only for patch artifacts: the list of touched files and a parsed
  // diffstat so job cards can show "3 files +40 -12" instead of truncated text.
  files?: string[] | null;
  diffstat?: { files: number; insertions: number; deletions: number } | null;
  // Verification verdicts: "result" is failed/blocked/pass, "failure" is the
  // machine class (no_model, billing_or_quota, ...). Used to render a swarm
  // whose every worker fast-failed as a failed run, not a green "done".
  result?: string;
  failure?: string;
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
    tokens_cached?: number;
    cache_savings_usd?: number;
    routing_saved_usd?: number;
    cache_saved_usd_swarm?: number;
    tool_output_tokens_saved?: number;
    tool_output_savings_usd?: number;
    tool_output_compactions?: number;
  };
  jobs: Job[];
};
export type WorkspaceInfo = {
  repo: string;
  branch: string;
  is_git: boolean;
  codegraph_status: string;
  recents?: string[];
  home?: string;
};

export type Workspace = { name: string; branch: string; active: boolean; dirty?: boolean };
export type Session = { id: string; title: string; created: number; active?: boolean; archived?: boolean; repo?: string; branch?: string; workspace_root?: string; input_tokens?: number; output_tokens?: number; cache_read_tokens?: number; estimated_cost_usd?: number; preview?: string };

export type SessionState = {
  state: "idle" | "thinking" | "awaiting_swarm";
  pending_swarms: boolean;
  // Set when the transcript ends on an unanswered user turn while idle -- the
  // signal to auto-continue after a backend restart (self-edit apply).
  resume_pending?: boolean;
  // Per-session runner liveness from SessionRunnerRegistry (multi-session Phase B).
  // "attaching" = deferred cold pilot build (not a user turn — no thinking chrome).
  runners?: Record<string, "running" | "idle" | "attaching" | "missing">;
  // Active VIEW session id — StatusBar must not treat background runners as
  // the active view thinking.
  active_view_id?: string | null;
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
    /** provider = billed usage.cost; estimated = token*catalog; mixed = both; plan_estimated = subscription credits (no API receipt). */
    cost_source?: "provider" | "estimated" | "mixed" | "plan_estimated";
    driver: string;
    price_in: number;
    price_out: number;
    tokens_cached?: number;
    cache_savings_usd?: number;
    /** Router baseline-vs-chosen savings (balanced/cheap policies only). */
    routing_saved_usd?: number;
    /** Swarm prompt-cache savings priced from usage artifacts x registry. */
    cache_saved_usd_swarm?: number;
    tool_output_tokens_saved?: number;
    tool_output_savings_usd?: number;
    tool_output_compactions?: number;
    history_compactions?: number;
    history_tokens_saved?: number;
    spill_count?: number;
    spill_chars?: number;
    evals_recorded?: number;
    evals_failed?: number;
    memory_layers?: Record<string, { bytes?: number; entries?: number; components?: Record<string, number> }>;
    compaction_advice?: {
      level?: string;
      hot_ratio?: number;
      l1_bytes?: number;
      l3_reclaimed_bytes?: number;
      reasons?: string[];
      needs_intervention?: boolean;
      warning_reason?: string;
    };
    history_compaction_ran?: boolean;
  };
  // Lifetime running total for the active chat session (persisted across
  // app restarts/updates, unlike `session` which is boot-scoped).
  session_total?: {
    session_id: string;
    est_cost_usd: number;
    input_tokens: number;
    output_tokens: number;
  } | null;
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
  session_id?: string;
  repo_hash?: string;
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
  status: "ready" | "indexing" | "unsupported" | "needs_scope" | "none";
  reason?: string | null;
  preflight?: {
    verdict?: string;
    reason?: string;
    suggested_roots?: string[];
    suggested_excludes?: string[];
  } | null;
  suggested_action?: {
    kind?: string;
    path?: string;
    excludes?: string[];
  } | null;
  nodes: number | null;
  edges: number | null;
  files: number | null;
  languages: string[] | null;
  last_indexed: string | null;
  repo: string;
};

export type WikiGraphData = {
  configured: boolean;
  status: "ok" | "not_configured" | "error" | "needs_auth";
  nodes: { id: string; title: string; section?: string; tags?: string[] }[];
  edges: { source: string; target: string }[];
  error?: string;
  base_url?: string;
  hint?: string;
  viewer_tier?: string;
  viewer_is_owner?: boolean;
  needs_owner_token?: boolean;
  page_count?: number;
};

/** Lightweight wiki strip payload (counts only; no full graph). */
export type WikiStatusData = {
  configured: boolean;
  status: "ok" | "not_configured" | "error" | "needs_auth";
  page_count: number;
  link_count: number;
  error?: string;
  retryable?: boolean;
  base_url?: string;
  hint?: string;
  viewer_tier?: string;
  viewer_is_owner?: boolean;
  needs_owner_token?: boolean;
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
  history_compactions?: number;
  history_tokens_saved?: number;
  spill_count?: number;
  spill_chars?: number;
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
  getBedrockStatus: () => getJSON<BedrockStatus>("/api/bedrock"),
  setBedrockCredentials: (creds: BedrockCredentials) =>
    postJSON<BedrockStatus & { ok?: boolean }>("/api/bedrock", creds),
  clearBedrockCredentials: () =>
    postJSON<BedrockStatus & { ok?: boolean }>("/api/bedrock", { clear: true }),
  getAuthPools: () => getJSON<AuthPoolsResponse>("/api/auth/pools"),
  getAuthPool: (provider: string) =>
    getJSON<AuthPoolPublic>(`/api/auth/pools?provider=${encodeURIComponent(provider)}`),
  addAuthPoolKey: (provider: string, api_key: string, label?: string) =>
    postJSON<AuthPoolPublic & { ok?: boolean; entry_id?: string }>("/api/auth/pools/add", {
      provider,
      type: "api_key",
      api_key,
      label: label || "",
    }),
  removeAuthPoolEntry: (provider: string, entry_id: string) =>
    postJSON<AuthPoolPublic & { ok?: boolean }>("/api/auth/pools/remove", {
      provider,
      entry_id,
    }),
  setAuthPoolStrategy: (provider: string, strategy: string) =>
    postJSON<AuthPoolPublic & { ok?: boolean }>("/api/auth/pools/strategy", {
      provider,
      strategy,
    }),
  resetAuthPool: (provider: string) =>
    postJSON<AuthPoolPublic & { ok?: boolean }>("/api/auth/pools/reset", { provider }),
  startAuthOAuth: (provider: string, label?: string) =>
    postJSON<{
      ok?: boolean;
      session_id: string;
      provider: string;
      user_code?: string;
      verification_uri?: string;
      verification_uri_complete?: string;
      auth_url?: string;
      flow?: string;
      interval?: number;
      expires_in?: number;
      hint?: string;
      error?: string;
    }>("/api/auth/oauth/start", { provider, label: label || "" }),
  pollAuthOAuth: (session_id: string, provider?: string) =>
    postJSON<{
      status: "pending" | "done" | "error";
      provider?: string;
      entry_id?: string;
      label?: string;
      error?: string;
      user_code?: string;
      verification_uri?: string;
      entries?: PoolEntryPublic[];
    }>("/api/auth/oauth/poll", { session_id, provider: provider || "" }),
  completeAuthOAuth: (session_id: string, code: string, provider = "anthropic") =>
    postJSON<{
      status: "pending" | "done" | "error";
      provider?: string;
      entry_id?: string;
      label?: string;
      error?: string;
      entries?: PoolEntryPublic[];
    }>("/api/auth/oauth/complete", { session_id, code, provider }),
  cancelAuthOAuth: (session_id: string, provider?: string) =>
    postJSON<{ ok?: boolean; status?: string; cleared?: boolean; error?: string }>(
      "/api/auth/oauth/cancel",
      { session_id, provider: provider || "" },
    ),
  getCursorCliStatus: (opts?: { refresh?: boolean }) =>
    postJSON<{
      ok?: boolean;
      installed?: boolean;
      authenticated?: boolean;
      binary?: string | null;
      label?: string;
      error?: string;
      install_hint?: string;
      auth_kind?: string;
    }>("/api/auth/cursor-cli/status", { refresh: !!opts?.refresh }),
  startCursorCliLogin: (opts?: { workspace?: string }) =>
    postJSON<{
      ok?: boolean;
      launched?: boolean;
      command?: string;
      hint?: string;
      error?: string;
      install_hint?: string;
      poll_interval?: number;
      expires_in?: number;
      auth_kind?: string;
      workspace?: string | null;
    }>("/api/auth/cursor-cli/login", {
      workspace: opts?.workspace || "",
      workspace_root: opts?.workspace || "",
    }),
  trustCursorCliWorkspace: (opts?: { workspace?: string }) =>
    postJSON<{
      ok?: boolean;
      trusted?: boolean;
      workspace?: string | null;
      error?: string;
    }>("/api/auth/cursor-cli/trust", {
      workspace: opts?.workspace || "",
      workspace_root: opts?.workspace || "",
    }),
  logoutCursorCli: () =>
    postJSON<{ ok?: boolean; error?: string }>("/api/auth/cursor-cli/logout", {}),
  getCursorCliModels: () =>
    postJSON<{ ok?: boolean; models?: { id: string }[]; auth_kind?: string; error?: string }>(
      "/api/auth/cursor-cli/models",
      {},
    ),
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
  jobs: (repoRoot?: string) => {
    const path = repoRoot
      ? `/api/jobs?repo=${encodeURIComponent(repoRoot)}`
      : "/api/jobs";
    return getJSON<Job[]>(path);
  },
  swarmLive: (repoRoot?: string) => {
    let path = withToken("/api/swarm/live");
    if (repoRoot) {
      path += `${path.includes("?") ? "&" : "?"}repo=${encodeURIComponent(repoRoot)}`;
    }
    return getJSON<SwarmLive>(path);
  },
  swarmCancel: (jobId: string) =>
    postJSON<{ ok: boolean; job_id?: string; error?: string }>(withToken("/api/swarm/cancel"), { job_id: jobId }),
  artifacts: (jobId: string) => getJSON<Artifact[]>(`/api/artifacts?job_id=${encodeURIComponent(jobId)}`),
  workspaces: () => getJSON<Workspace[]>("/api/workspaces"),
  switchWorkspace: (name: string, opts?: { allow_dirty?: boolean }) =>
    postJSON<{ ok: boolean; active?: string; error?: string; dirty?: boolean }>(
      "/api/workspaces/switch",
      { name, allow_dirty: !!opts?.allow_dirty },
    ),
  createWorkspace: (name: string, branch?: string) =>
    postJSON<{ ok: boolean; active?: string; error?: string }>(
      "/api/workspaces/create",
      { name, branch },
    ),
  sessions: (repoRoot?: string) => {
    const path = repoRoot
      ? `/api/sessions?repo=${encodeURIComponent(repoRoot)}`
      : "/api/sessions";
    return getJSON<Session[]>(path);
  },
  sessionsBank: (opts?: { query?: string; limit?: number }) => {
    const params = new URLSearchParams({ all: "1" });
    if (opts?.query) params.set("q", opts.query);
    if (opts?.limit != null) params.set("limit", String(opts.limit));
    return getJSON<Session[]>(`/api/sessions?${params.toString()}`);
  },
  /** FTS5 session recall (Wave D). Empty query returns [] without calling the API. */
  searchSessions: async (query: string, limit = 20): Promise<SessionSearchHit[]> => {
    const qs = buildSessionSearchQuery(query, limit);
    if (!qs) return [];
    const raw = await getJSON<unknown>(`/api/sessions/search?${qs}`);
    return normalizeSessionSearchHits(raw);
  },
  sessionTranscript: (session: string) => getJSON<{ history: any[]; display?: any[]; job_ids?: string[] }>(withToken(`/api/sessions/transcript?session=${encodeURIComponent(session)}`)),
  getSessionState: () => getJSON<SessionState>(withToken("/api/session/state")),
  /** Hard-stop a turn. Pass sessionId to target a background runner without view attach. */
  interruptSession: (sessionId?: string) =>
    postJSON<{ ok: boolean }>(
      "/api/session/interrupt",
      sessionId ? { session_id: sessionId } : {},
    ),
  rewindSession: (userOrdinal: number) =>
    postJSON<{
      ok: boolean;
      prefill?: string;
      notice?: string;
      removed_count?: number;
      error?: string;
      code?: string;
    }>("/api/session/rewind", { user_ordinal: userOrdinal }),
  restoreRewind: () =>
    postJSON<{
      ok: boolean;
      display?: any[];
      history?: any[];
      error?: string;
      code?: string;
    }>("/api/session/rewind/restore", {}),
  getSwarmResults: () => getJSON<SwarmResultsResponse>(withToken("/api/session/swarm-results")),
  createSession: (title?: string) => postJSON<Session>("/api/sessions/create", { title }),
  switchSession: (id: string) => postJSON("/api/sessions/switch", { id }),
  relocateSession: (workspaceRoot: string, opts?: { sessionId?: string; title?: string }) =>
    postJSON<{
      ok: boolean;
      active?: string;
      repo?: string;
      workspace_root?: string;
      session?: Session;
      error?: string;
      codegraph?: string;
    }>("/api/sessions/relocate", {
      workspace_root: workspaceRoot,
      session_id: opts?.sessionId,
      title: opts?.title,
    }),
  // POST rather than DELETE: the packaged Electron preload only bridges
  // getJSON/postJSON, so a DELETE falls through to an unroutable fetch.
  deleteSession: (id: string) =>
    postJSON<{ ok: boolean; active: string | null }>("/api/sessions/delete", { id }),
  clearSessions: () =>
    postJSON<{ ok: boolean; deleted: number; active: string | null }>(withToken("/api/sessions/clear"), {}),
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
  /** Mid-turn SSE reattach: replay retained frames since ``since`` cursor. */
  chatEvents: (opts?: { session?: string; since?: number; generation?: number }) =>
    getJSON<ChatEventReplay>(chatEventsPath(opts || {})),
  mcp: () => getJSON<{ servers: any[]; tools: any[] }>("/api/mcp"),
  mcpCatalog: () => getJSON<{ catalog: Record<string, any> }>("/api/mcp/catalog"),
  mcpAdd: (name: string, command?: string, args?: string[], env?: Record<string, string>, url?: string) => {
    const payload = url ? { name, url } : { name, command, args, env };
    return postJSON<{ ok: boolean; tools?: number; error?: string }>("/api/mcp/add", payload);
  },
  mcpRemove: (name: string) => postJSON<{ ok: boolean }>("/api/mcp/remove", { name }),
  mcpStart: (name: string) => postJSON<{ ok: boolean; tools?: number; error?: string }>("/api/mcp/start", { name }),
  mcpStop: (name: string) => postJSON<{ ok: boolean }>("/api/mcp/stop", { name }),
  mcpRefresh: (name: string) =>
    postJSON<{ ok: boolean; tools?: number; error?: string }>("/api/mcp/refresh", { name }),
  skills: () => getJSON<any[]>("/api/skills"),
  skillDistill: () => postJSON<{ skill?: any; rules?: any }>("/api/skills/distill", {}),
  wikiIngestPrepared: (pages: any[]) => postJSON<{ ok: boolean; ingested: number }>("/api/wiki/ingest-prepared", { pages }),
  modelCatalog: (opts?: { refresh?: boolean }) =>
    getJSON<ModelCatalogResponse>(
      opts?.refresh ? "/api/models/catalog?refresh=1" : "/api/models/catalog",
    ),
  toggleModel: (spec: string, enabled: boolean) =>
    postJSON<{ ok: boolean; enabled: string[]; driver?: string; driver_changed?: boolean }>(
      "/api/models/toggle",
      { spec, enabled },
    ),
  setEnabledModels: (enabled: string[]) =>
    postJSON<{ ok: boolean; enabled: string[]; driver?: string; driver_changed?: boolean }>(
      "/api/models/set",
      { enabled },
    ),
  rules: () => getJSON<any[]>("/api/rules"),
  ruleAdd: (text: string, scope?: string) =>
    postJSON<{ ok: boolean; slug: string; text: string; scope: string; state: string; source: string }>(
      "/api/rules/add", { text, scope: scope || "global" }),
  ruleUpdate: (slug: string, patch: { text?: string; scope?: string }) =>
    postJSON<{ ok: boolean; slug: string; text: string; scope: string; state: string }>(
      "/api/rules/update", { slug, ...patch }),
  ruleRemove: (slug: string) => postJSON<{ ok: boolean }>("/api/rules/remove", { slug }),
  ruleApprove: (slug: string) => postJSON<{ ok: boolean }>("/api/rules/approve", { slug }),
  ruleReject: (slug: string) => postJSON<{ ok: boolean }>("/api/rules/reject", { slug }),
  skillAdd: (name: string, description: string, body: string) =>
    postJSON<{ ok: boolean; slug: string; name: string; state: string; source: string }>(
      "/api/skills/add", { name, description, body }),
  skillUpdate: (slug: string, patch: { name?: string; description?: string; body?: string }) =>
    postJSON<{ ok: boolean; slug: string; name: string; description: string; state: string }>(
      "/api/skills/update", { slug, ...patch }),
  skillRemove: (slug: string) => postJSON<{ ok: boolean }>("/api/skills/remove", { slug }),
  skillApprove: (slug: string) => postJSON<{ ok: boolean }>("/api/skills/approve", { slug }),
  skillReject: (slug: string) => postJSON<{ ok: boolean }>("/api/skills/reject", { slug }),
  memory: () => getJSON<{ memory: { id: string; text: string; category: string; created_at: number; source: string }[]; total_chars: number; limit: number }>("/api/memory"),
  memoryAdd: (text: string, category?: string) => postJSON<{ id: string; text: string; category: string }>("/api/memory/add", { text, category }),
  memoryRemove: (id: string) => postJSON<{ ok: boolean }>("/api/memory/remove", { id }),
  memoryProposeAccept: (id: string) =>
    postJSON<{ ok: boolean; id?: string; text?: string; category?: string; error?: string }>(
      "/api/memory/propose/accept",
      { id },
    ),
  memoryProposeDismiss: (id: string) =>
    postJSON<{ ok: boolean; error?: string }>("/api/memory/propose/dismiss", { id }),
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
  pruneEditBranches: () =>
    postJSON<{ ok: boolean; deleted: string[]; count: number }>("/api/worktrees/prune-edit-branches", {}),
  setWorktreeMax: (max: number) => postJSON<{ ok: boolean }>("/api/worktrees/max", { max }),

  openWorkspace: (path: string) => postJSON<{ ok: boolean; repo: string; branch: string; is_git: boolean; codegraph: "indexing" | "ready" | "unsupported" | "needs_scope" | "none" | "pending"; active_session?: string }>("/api/workspace/open", { path }),
  forgetWorkspace: (path: string) => postJSON<{ ok: boolean; recents: string[]; cleared_active?: boolean; repo?: string }>("/api/workspace/forget", { path }),
  getWorkspace: () => getJSON<WorkspaceInfo>("/api/workspace"),
  getWorkspaceFiles: () =>
    getJSON<{ files: string[]; truncated?: boolean; total?: number; capped?: number }>(
      withToken("/api/workspace/files"),
    ),
  searchSymbols: (q: string) => getJSON<{ symbols: { name: string; kind: string; path: string; line: number }[]; status?: string }>(withToken("/api/workspace/symbols?q=" + encodeURIComponent(q))),
  readFile: (path: string) =>
    getJSONSoft<{
      ok: boolean;
      path?: string;
      content?: string;
      truncated?: boolean;
      error?: string;
      binary?: boolean;
      name?: string;
      size?: number;
      mime?: string;
      ext?: string;
      sqlite_tables?: string[];
    }>("/api/file/read?path=" + encodeURIComponent(path)),
  /** Tokened absolute URL for PDF/image/HTML iframe or <img> preview. */
  fileRawUrl: (path: string): string => {
    const rel = withToken("/api/file/raw?path=" + encodeURIComponent(path));
    if (typeof window !== "undefined") {
      const port = (window as any).__HARNESS_PORT__;
      if (port) return `http://127.0.0.1:${port}${rel}`;
    }
    return rel;
  },
  writeFile: (path: string, content: string) => postJSON<{ ok: boolean; bytes?: number; error?: string }>("/api/file/write", { path, content }),
  deleteFile: (path: string) =>
    postJSON<{ ok: boolean; path?: string; error?: string }>("/api/file/delete", { path }),
  renameFile: (args: { path: string; new_name: string } | { from: string; to: string }) =>
    postJSON<{ ok: boolean; from?: string; to?: string; error?: string }>("/api/file/rename", args),
  mkdir: (path: string) =>
    postJSON<{ ok: boolean; path?: string; error?: string }>("/api/file/mkdir", { path }),
  inlineEdit: (path: string, selection: string, instruction: string, prefix: string, suffix: string, language: string) => postJSON<{ ok: boolean; edit?: string; error?: string }>("/api/inline-edit", { path, selection, instruction, prefix, suffix, language }),
  compactSession: () => postJSON<{ ok: boolean; before_tokens: number; after_tokens: number }>("/api/session/compact", {}),
  steerSession: (text: string, images?: string[]) =>
    postJSON<{ ok: boolean }>("/api/session/steer", { text, images: images && images.length ? images : undefined }),
  // PROMPT QUEUE: a "playlist" of full user prompts that each run as their own
  // complete turn one after the previous fully finishes. Distinct from steer
  // (a mid-turn interrupt on the CURRENT running turn). Items can be edited /
  // removed / reordered before they run.
  queueList: () => getJSON<{ items: { id: string; text: string; images?: string[]; model?: string }[] }>(withToken("/api/session/queue")),
  queueAdd: (text: string, images?: string[]) => postJSON<{ ok: boolean; item: { id: string; text: string; images?: string[]; model?: string } }>("/api/session/queue", { text, images: images || [] }),
  queueRemove: (id: string) => postJSON<{ ok: boolean; id: string }>("/api/session/queue", { id }),
  queueReorder: (ids: string[]) => postJSON<{ ok: boolean; items: { id: string; text: string; images?: string[]; model?: string }[] }>("/api/session/queue/reorder", { ids }),
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
  applyCodegraphExcludes: (excludes?: string[]) =>
    postJSON<{ ok: boolean; status: string; reason?: string }>("/api/codegraph/apply-excludes", {
      excludes: excludes || [],
    }),
  getWikiGraph: () => getJSON<WikiGraphData>("/api/wiki/graph"),
  getWikiStatus: () => getJSON<WikiStatusData>("/api/wiki/status"),
  getWikiConfig: () => getJSON<{ api_base: string; has_token: boolean }>("/api/wiki/config"),
  setWikiConfig: (api_base?: string, owner_token?: string) =>
    postJSON<{ api_base: string; has_token: boolean }>("/api/wiki/config", { api_base, owner_token }),
  disconnectWiki: () =>
    postJSON<{ api_base: string; has_token: boolean }>("/api/wiki/disconnect", {}),
  startWikiHandoff: () =>
    postJSON<{
      ok: boolean;
      nonce: string;
      return_url: string;
      setup_url: string;
    }>("/api/wiki/handoff", {}),

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
