// Typed harness API -- thin wrappers over the transport seam.
import { getJSON, postJSON, stream, withToken, type StreamEvent } from "./transport";

export type Config = {
  driver: string; reach: string; budget: number;
  models?: string[]; preflight?: string | null;
  repo?: string;
};
export type Settings = {
  driver: string;
  reach: string;
  budget: number;
  models: string[];
  auto_distill: boolean;
  reviewEditsBeforeApply?: boolean;
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
export type Job = { id: string; goal: string; status: string };
export type Artifact = { type: string; headline: string; confidence?: number };
export type Workspace = { name: string; branch: string; active: boolean; dirty?: boolean };
export type Session = { id: string; title: string; created: number; active?: boolean; archived?: boolean; repo?: string; branch?: string };

export type SessionState = {
  state: "idle" | "thinking" | "awaiting_swarm";
  pending_swarms: boolean;
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
  env_var: string;
  base_url: string;
  has_key: boolean;
  api_mode: string;
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

export type CodegraphStatus = {
  indexed: boolean;
  status: "ready" | "indexing" | "unsupported" | "none";
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

export const api = {
  providers: () => getJSON<ProviderInfo[]>("/api/providers"),
  probeProvider: (provider: string) => postJSON<ProbeResult>("/api/providers/probe", { provider }),
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
  artifacts: (jobId: string) => getJSON<Artifact[]>(`/api/artifacts?job_id=${encodeURIComponent(jobId)}`),
  workspaces: () => getJSON<Workspace[]>("/api/workspaces"),
  switchWorkspace: (name: string) => postJSON("/api/workspaces/switch", { name }),
  createWorkspace: (name: string, branch?: string) =>
    postJSON("/api/workspaces/create", { name, branch }),
  sessions: () => getJSON<Session[]>("/api/sessions"),
  sessionTranscript: (session: string) => getJSON<{ history: any[] }>(withToken(`/api/sessions/transcript?session=${encodeURIComponent(session)}`)),
  getSessionState: () => getJSON<SessionState>(withToken("/api/session/state")),
  interruptSession: () => postJSON<{ ok: boolean }>("/api/session/interrupt", {}),
  getSwarmResults: () => getJSON<SwarmResultsResponse>(withToken("/api/session/swarm-results")),
  createSession: (title?: string) => postJSON<Session>("/api/sessions/create", { title }),
  switchSession: (id: string) => postJSON("/api/sessions/switch", { id }),
  deleteSession: (id: string) => postJSON<{ ok: boolean; active: string | null }>("/api/sessions/delete", { session: id }),
  archiveSession: (id: string, archived: boolean) => postJSON<{ ok: boolean }>("/api/sessions/archive", { session: id, archived }),
  renameSession: (id: string, title: string) => postJSON<{ ok: boolean }>("/api/sessions/rename", { session: id, title }),
  swapPilot: (model: string) => getJSON(withToken(`/api/pilot?model=${encodeURIComponent(model)}`)),
  chat: (message: string, onEvent: (e: StreamEvent) => void, onDone?: () => void, onError?: (e: any) => void, plan: boolean = false) =>
    stream(`/api/chat?message=${encodeURIComponent(message)}${plan ? "&plan=true" : ""}`, onEvent, onDone, onError),
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
  rules: () => getJSON<any[]>("/api/rules"),
  ruleApprove: (slug: string) => postJSON<{ ok: boolean }>("/api/rules/approve", { slug }),
  ruleReject: (slug: string) => postJSON<{ ok: boolean }>("/api/rules/reject", { slug }),
  skillApprove: (slug: string) => postJSON<{ ok: boolean }>("/api/skills/approve", { slug }),
  skillReject: (slug: string) => postJSON<{ ok: boolean }>("/api/skills/reject", { slug }),
  auto: (objective: string, onEvent: (e: StreamEvent) => void, onDone?: () => void, onError?: (e: any) => void) =>
    stream(`/api/auto?objective=${encodeURIComponent(objective)}`, onEvent, onDone, onError),
  exportUrl: (sessionId: string, format: "md" | "json") =>
    withToken(`/api/sessions/export?session=${encodeURIComponent(sessionId)}&format=${format}`),

  getWorktrees: () => getJSON<{ worktrees: Worktree[]; max: number }>("/api/worktrees"),
  addWorktree: (branch: string, base?: string) => postJSON<Worktree>("/api/worktrees/add", { branch, base }),
  removeWorktree: (path: string, force?: boolean) => postJSON<{ ok: boolean }>("/api/worktrees/remove", { path, force }),
  pruneWorktrees: () => postJSON<{ ok: boolean }>("/api/worktrees/prune", {}),
  setWorktreeMax: (max: number) => postJSON<{ ok: boolean }>("/api/worktrees/max", { max }),

  openWorkspace: (path: string) => postJSON<{ ok: boolean; repo: string; branch: string; is_git: boolean; codegraph: "indexing" | "ready" | "unsupported" }>("/api/workspace/open", { path }),
  getWorkspace: () => getJSON<{ repo: string; branch: string; is_git: boolean; codegraph_status: string; recents?: string[] }>("/api/workspace"),
  getWorkspaceFiles: () => getJSON<{ files: string[] }>(withToken("/api/workspace/files")),
  compactSession: () => postJSON<{ ok: boolean; before_tokens: number; after_tokens: number }>("/api/session/compact", {}),

  getCheckpoints: () => getJSON<Checkpoint[]>(withToken("/api/checkpoints")),
  restoreCheckpoint: (id: string) => postJSON<{ ok: boolean; restored_files: string[]; auto_snapshot_id: string }>("/api/checkpoints/restore", { id }),
  snapshotCheckpoint: (label: string) => postJSON<{ ok: boolean; id: string }>("/api/checkpoints/snapshot", { label }),

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

  getReviews: () => getJSON<PendingReview[]>(withToken("/api/reviews")),
  applyReview: (id: string, decisions: Record<string, "accept" | "reject">) =>
    postJSON<{ ok: boolean; applied_files: string[]; rejected_hunks: string[]; checkpoint_id: string | null; message: string }>("/api/reviews/apply", { id, decisions }),
  dismissReview: (id: string) => postJSON<{ ok: boolean }>("/api/reviews/dismiss", { id }),
};
