/** Live agent-command sessions, keyed the way Hermes keys `procId`.

Chat tokens become command links only when a real run_command / shell card
has registered here. Speculative `` `git pull` `` stays prose until that
process exists; then the click focuses that mirror, not a blank tab.
*/

export type AgentCommandSession = {
  id: string;
  command: string;
  output: string;
  state: "running" | "done" | "failed";
  updatedAt: number;
  /** True only when this process observed the command running. */
  railVisible?: boolean;
  /** Harness chat that ran this command. Missing means unscoped leftover. */
  sessionId?: string;
};

const byId = new Map<string, AgentCommandSession>();
/** Normalized command → session ids, most recently updated last. */
const idsByCommand = new Map<string, string[]>();

let version = 0;
const listeners = new Set<() => void>();

function emit(bump: boolean): void {
  if (bump) version += 1;
  listeners.forEach((fn) => fn());
}

/** Collapse whitespace and a leading `$ ` so `$ git pull` matches `git pull`. */
export function normalizeCommandKey(command: string): string {
  return (command || "")
    .trim()
    .replace(/^\$\s+/, "")
    .replace(/\s+/g, " ");
}

function rememberCommandId(key: string, id: string): void {
  const prev = idsByCommand.get(key) || [];
  idsByCommand.set(key, [...prev.filter((item) => item !== id), id]);
}

function forgetCommandId(key: string, id: string): void {
  const next = (idsByCommand.get(key) || []).filter((item) => item !== id);
  if (next.length) idsByCommand.set(key, next);
  else idsByCommand.delete(key);
}

/**
 * Record a live or completed agent command. Same id + command updates output
 * silently (streaming) so chat markdown does not remount every chunk.
 * A new id or a command change bumps the version so existing tokens light up.
 */
export function registerAgentCommandSession(input: {
  id: string;
  command: string;
  output?: string;
  state?: AgentCommandSession["state"];
  sessionId?: string;
}): AgentCommandSession | null {
  const id = String(input.id || "").trim();
  const command = normalizeCommandKey(input.command);
  if (!id || !command || command.length > 500) return null;
  const output = String(input.output || "");
  const state = input.state;
  const sessionId = String(input.sessionId || "").trim();
  const existing = byId.get(id);
  const now = Date.now();
  if (existing && existing.command === command) {
    const prevState = existing.state;
    let nextState = prevState;
    if (state && prevState === "running") nextState = state;
    else if (state === "failed" && prevState === "done") nextState = "failed";
    existing.output = output;
    existing.state = nextState;
    if (sessionId) existing.sessionId = sessionId;
    if (nextState === "running") existing.railVisible = true;
    const stateChanged = nextState !== prevState;
    if (stateChanged) {
      existing.updatedAt = now;
      emit(true);
      rememberCommandId(command, id);
      return existing;
    }
    rememberCommandId(command, id);
    return existing;
  }
  if (existing && existing.command !== command) {
    forgetCommandId(existing.command, id);
  }
  const session: AgentCommandSession = {
    id,
    command,
    output,
    state: state || "running",
    updatedAt: now,
    railVisible: !state || state === "running",
    ...(sessionId ? { sessionId } : {}),
  };
  byId.set(id, session);
  rememberCommandId(command, id);
  emit(true);
  return session;
}

export function lookupAgentCommandSession(command: string): AgentCommandSession | null {
  const key = normalizeCommandKey(command);
  if (!key) return null;
  const ids = idsByCommand.get(key);
  if (!ids || ids.length === 0) return null;
  return byId.get(ids[ids.length - 1]) ?? null;
}

export function lookupAgentCommandSessionById(id: string): AgentCommandSession | null {
  const key = String(id || "").trim();
  if (!key) return null;
  return byId.get(key) ?? null;
}

export function subscribeAgentCommandIndex(onStoreChange: () => void): () => void {
  listeners.add(onStoreChange);
  return () => {
    listeners.delete(onStoreChange);
  };
}

export function getAgentCommandIndexVersion(): number {
  return version;
}

export function dismissAgentCommandSession(id: string): boolean {
  const key = String(id || "").trim();
  const existing = byId.get(key);
  if (!existing) return false;
  forgetCommandId(existing.command, key);
  byId.delete(key);
  emit(true);
  return true;
}

export function listAgentCommandSessions(sessionId?: string): AgentCommandSession[] {
  const all = [...byId.values()].sort((a, b) => b.updatedAt - a.updatedAt);
  if (sessionId === undefined) return all;
  const sid = sessionId.trim();
  if (!sid) return [];
  return all.filter((session) => session.sessionId === sid);
}

/** Test helper: wipe the index between cases. */
export function _resetAgentCommandIndexForTests(): void {
  byId.clear();
  idsByCommand.clear();
  version = 0;
  listeners.clear();
}
