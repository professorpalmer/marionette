/** Live agent-terminal mirror backlog (Hermes-style, Marionette-owned).

Read-only xterm views register a writer keyed by process id. Chunks append to a
capped backlog so a tab opened mid-stream (or reopened) can replay history.
Interactive ConPTY is a separate surface — this module never talks to it.
*/

type Writer = (chunk: string) => void;

const writers = new Map<string, Writer>();
const backlog = new Map<string, string>();
const commandHeaders = new Map<string, string>();
const lastSnapshots = new Map<string, string>();
const seededCommands = new Set<string>();

const MAX_BACKLOG = 256_000;

/** Register an xterm write callback and replay backlog. Returns unregister. */
export function registerAgentTerminalWriter(procId: string, write: Writer): () => void {
  writers.set(procId, write);
  const history = backlog.get(procId);
  if (history) write(history);
  return () => {
    if (writers.get(procId) === write) writers.delete(procId);
  };
}

/** Append a streamed chunk to backlog (capped) and the live writer, if any. */
export function writeAgentTerminalChunk(procId: string, chunk: string): void {
  if (!procId || !chunk) return;
  const next = (backlog.get(procId) ?? "") + chunk;
  backlog.set(procId, next.length > MAX_BACKLOG ? next.slice(-MAX_BACKLOG) : next);
  writers.get(procId)?.(chunk);
}

/** Seed `$ command` so an agent mirror never opens as an empty void. */
export function seedAgentTerminalCommand(procId: string, command: string): void {
  const trimmed = command.trim();
  if (!procId || !trimmed || seededCommands.has(procId)) return;
  seededCommands.add(procId);
  const header = `$ ${trimmed}\r\n`;
  commandHeaders.set(procId, header);
  writeAgentTerminalChunk(procId, header);
}

/**
 * Ingest a full output snapshot. Appends only the delta when possible; resets
 * to the rolling tail when the snapshot slid.
 */
export function syncAgentTerminalSnapshot(procId: string, output: string): void {
  if (!procId || !output) return;

  const current = backlog.get(procId) ?? "";
  const header = commandHeaders.get(procId) ?? "";
  const body = header && current.startsWith(header) ? current.slice(header.length) : current;
  const previous = lastSnapshots.get(procId) ?? "";

  if (output === previous || output === body || body.endsWith(output)) {
    lastSnapshots.set(procId, output);
    return;
  }

  if (output.startsWith(previous)) {
    writeAgentTerminalChunk(procId, output.slice(previous.length));
    lastSnapshots.set(procId, output);
    return;
  }

  if (output.startsWith(body)) {
    writeAgentTerminalChunk(procId, output.slice(body.length));
    lastSnapshots.set(procId, output);
    return;
  }

  const next = `${header}${output}`.slice(-MAX_BACKLOG);
  lastSnapshots.set(procId, output);
  backlog.set(procId, next);
  writers.get(procId)?.(`\x1bc${next}`);
}

/** Test helper: clear all stream state between cases. */
export function _resetAgentTerminalStreamForTests(): void {
  writers.clear();
  backlog.clear();
  commandHeaders.clear();
  lastSnapshots.clear();
  seededCommands.clear();
}

/** Test helper: inspect backlog for a proc id. */
export function _agentTerminalBacklogForTests(procId: string): string {
  return backlog.get(procId) ?? "";
}
