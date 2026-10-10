import { useEffect, useRef, useState } from "react";
import { Terminal, type ITerminalInitOnlyOptions, type ITerminalOptions } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { RotateCw } from "lucide-react";
import { postJSON, stream } from "../lib/transport";
import { isExternalUrl, looksLikeFilePath, openAgentFile, openAgentUrl } from "../lib/agentLinks";
import {
  registerAgentTerminalWriter,
  seedAgentTerminalCommand,
  syncAgentTerminalSnapshot,
} from "../lib/agentTerminalStream";
import { registerTerminalPathLinks } from "../lib/terminalPathLinks";
import {
  addToChatShortcutHint,
  dispatchAddTerminalSelection,
  isAddSelectionShortcut,
  isMacNavigator,
  readLiveTerminalSelection,
  readTerminalSelectionPosition,
  terminalSelectionAnchor,
  terminalSelectionLabel,
} from "../lib/terminalSelection";
import { hostHasLayout, safePtyDims } from "./terminalDims";
import {
  decodeTerminalStreamEvent,
  terminalBareOnDoneAction,
  terminalMissingSessionAction,
  terminalNotice,
  terminalEventIsCurrent,
  terminalObservationLabel,
  type TerminalObservation,
  terminalStreamPath,
} from "./terminalStreamPolicy";

/** https via WebLinksAddon + workspace/file:// paths via our ILinkProvider. */
function attachTerminalLinkHandlers(term: Terminal): void {
  term.loadAddon(
    new WebLinksAddon((_event, uri) => {
      if (isExternalUrl(uri)) openAgentUrl(uri);
      else if (looksLikeFilePath(uri)) openAgentFile(uri);
    }),
  );
  // WebLinksAddon cannot surface bare paths (hard URL filter); own provider.
  registerTerminalPathLinks(term);
}

type AgentView = { id: string; command: string };

/**
 * Read-only agent mirror. Command output comes from a pipe, not a PTY, so
 * its lines end in a bare LF. Without convertEol each line starts at the
 * column where the previous line ended.
 */
export const AGENT_TERMINAL_OPTIONS: ITerminalOptions & ITerminalInitOnlyOptions = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: 12,
  theme: {
    background: "#0a0a0c",
    foreground: "#d4d4d8",
    cursor: "#7c8cff",
    selectionBackground: "#2a2a3a",
  },
  cursorBlink: false,
  disableStdin: true,
  convertEol: true,
  scrollback: 5000,
  cols: 80,
  rows: 24,
};

// Built-in terminal: xterm.js front-end over the harness PTY backend.
// create -> SSE stream output (base64 frames) -> POST keystrokes -> resize -> kill.
// A restart counter lets the user relaunch a dead/stuck shell without reloading
// the app (the previous session is killed cleanly first).
// Agent-mirror mode overlays a read-only xterm for a captured run_command
// session without destroying the interactive ConPTY underneath.
export default function TerminalPane() {
  const hostRef = useRef<HTMLDivElement>(null);
  const agentHostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const agentTermRef = useRef<Terminal | null>(null);
  const agentFitRef = useRef<FitAddon | null>(null);
  const agentUnregisterRef = useRef<null | (() => void)>(null);
  const idRef = useRef<string>("");
  const cancelRef = useRef<null | (() => void)>(null);
  // Only a confirmed missing session permits automatic replacement.
  const autoRecoveredRef = useRef(false);
  // Bumping this re-runs the effect: cleanly tears down the old PTY + xterm and
  // spins up a fresh one. Drives the Restart button and exit auto-recovery.
  const [restartNonce, setRestartNonce] = useState(0);
  const [exited, setExited] = useState(false);
  const [observation, setObservation] = useState<{ state: TerminalObservation; at: number }>({ state: "unknown", at: Date.now() });
  const [now, setNow] = useState(Date.now());
  const [submission, setSubmission] = useState("");
  const submissionRef = useRef("");

  const writeInput = async (id: string, data: string) => {
    const submissionId = crypto.randomUUID();
    submissionRef.current = submissionId;
    setSubmission("input pending");
    const current = () => idRef.current === id && submissionRef.current === submissionId;
    const timeout = window.setTimeout(() => {
      if (current()) setSubmission("input unconfirmed");
    }, 5000);
    try {
      const receipt = await postJSON<{ id?: string; submission_id?: string; accepted_bytes?: number; ok?: boolean }>(
        "/api/terminal/write", { id, data, submission_id: submissionId },
      );
      if (current()) setSubmission(receipt.id === id && receipt.submission_id === submissionId && receipt.ok === true && receipt.accepted_bytes === new TextEncoder().encode(data).length
        ? "Input accepted" : "input not confirmed");
    } catch {
      if (current()) setSubmission("input failed or unconfirmed");
    } finally {
      window.clearTimeout(timeout);
    }
  };
  const [agentView, setAgentView] = useState<AgentView | null>(null);
  useEffect(() => {
    if (agentView || observation.state === "exited" || observation.state === "stale") return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      clearTimeout(timer);
      if (document.hidden || !hostRef.current || !hostHasLayout(hostRef.current)) return;
      const current = Date.now();
      setNow(current);
      const deadline = observation.at + (observation.state === "active_output" && current < observation.at + 1000 ? 1000 : 5001);
      if (deadline > current) timer = setTimeout(schedule, deadline - current);
    };
    schedule();
    document.addEventListener("visibilitychange", schedule);
    const resize = new ResizeObserver(schedule);
    if (hostRef.current) resize.observe(hostRef.current);
    return () => {
      clearTimeout(timer);
      resize.disconnect();
      document.removeEventListener("visibilitychange", schedule);
    };
  }, [agentView, observation]);
  const [selection, setSelection] = useState("");
  const [selectionStyle, setSelectionStyle] = useState<{ left: number; top: number } | null>(null);
  const agentViewRef = useRef<AgentView | null>(null);
  agentViewRef.current = agentView;

  const clearSelectionUi = () => {
    setSelection("");
    setSelectionStyle(null);
  };

  const addSelectionToChat = (term: Terminal | null, shellName: string) => {
    const text = readLiveTerminalSelection(term);
    if (!text.trim() || !term) return;
    const label = terminalSelectionLabel(
      text,
      shellName,
      readTerminalSelectionPosition(term),
    );
    dispatchAddTerminalSelection(text, label);
    try {
      term.clearSelection();
    } catch { /* ignore */ }
    clearSelectionUi();
  };

  const restart = () => {
    setExited(false);
    autoRecoveredRef.current = false;
    setRestartNonce((n) => n + 1);
  };

  const showInteractiveShell = () => {
    setAgentView(null);
  };

  // ActionCard "Run" injects a command into the live PTY.
  useEffect(() => {
    const onRun = (e: Event) => {
      const cmd = String((e as CustomEvent<{ command?: string }>).detail?.command || "").trim();
      if (!cmd) return;
      // Running into the user shell implies leaving the agent mirror.
      setAgentView(null);
      const id = idRef.current;
      if (!id) {
        try {
          termRef.current?.writeln(
            "\r\n\x1b[90m[no live shell -- press Restart, then Run again]\x1b[0m"
          );
        } catch { /* ignore */ }
        return;
      }
      // Send command + Enter. Prefer \r for PTY line discipline.
      void writeInput(id, cmd + "\r");
    };
    window.addEventListener("harness-run-command", onRun as EventListener);
    return () => window.removeEventListener("harness-run-command", onRun as EventListener);
  }, []);

  // Reveal a read-only agent command session (Hermes openAgentTerminal pattern).
  useEffect(() => {
    const onOpen = (e: Event) => {
      const detail = (e as CustomEvent<{ id?: string; command?: string; output?: string }>).detail || {};
      const id = String(detail.id || "").trim();
      const command = String(detail.command || "").trim();
      const output = String(detail.output || "");
      if (!id || !command) return;
      seedAgentTerminalCommand(id, command);
      if (output) syncAgentTerminalSnapshot(id, output);
      setAgentView({ id, command });
    };
    const onSync = (e: Event) => {
      const detail = (e as CustomEvent<{ id?: string; output?: string }>).detail || {};
      const id = String(detail.id || "").trim();
      const output = String(detail.output || "");
      if (id && output) syncAgentTerminalSnapshot(id, output);
    };
    window.addEventListener("harness-open-agent-terminal", onOpen as EventListener);
    window.addEventListener("harness-sync-agent-terminal", onSync as EventListener);
    return () => {
      window.removeEventListener("harness-open-agent-terminal", onOpen as EventListener);
      window.removeEventListener("harness-sync-agent-terminal", onSync as EventListener);
    };
  }, []);

  // Mount / swap the read-only agent xterm when agentView changes.
  useEffect(() => {
    if (!agentView) {
      agentUnregisterRef.current?.();
      agentUnregisterRef.current = null;
      return;
    }
    clearSelectionUi();
    const host = agentHostRef.current;
    if (!host) return;

    let term = agentTermRef.current;
    let fit = agentFitRef.current;
    if (!term) {
      term = new Terminal(AGENT_TERMINAL_OPTIONS);
      fit = new FitAddon();
      term.loadAddon(fit);
      attachTerminalLinkHandlers(term);
      term.open(host);
      agentTermRef.current = term;
      agentFitRef.current = fit;
    }

    try {
      fit?.fit();
    } catch { /* ignore */ }

    agentUnregisterRef.current?.();
    try {
      term.reset();
    } catch { /* ignore */ }
    agentUnregisterRef.current = registerAgentTerminalWriter(agentView.id, (chunk) => {
      try {
        term!.write(chunk);
      } catch { /* ignore */ }
    });

    const syncSelection = () => {
      const text = readLiveTerminalSelection(term);
      setSelection(text);
      setSelectionStyle(text.trim() ? terminalSelectionAnchor(host) : null);
    };
    const selectionSub = term.onSelectionChange(syncSelection);

    const ro = new ResizeObserver(() => {
      try {
        fit?.fit();
      } catch { /* ignore */ }
    });
    ro.observe(host);

    return () => {
      try { selectionSub.dispose(); } catch { /* ignore */ }
      ro.disconnect();
      agentUnregisterRef.current?.();
      agentUnregisterRef.current = null;
      clearSelectionUi();
    };
  }, [agentView]);

  // Dispose the agent xterm only when the pane unmounts (ConPTY stays warm across
  // tab swaps; agent backlog survives returning to the interactive shell).
  useEffect(() => {
    return () => {
      agentUnregisterRef.current?.();
      agentUnregisterRef.current = null;
      try {
        agentTermRef.current?.dispose();
      } catch { /* ignore */ }
      agentTermRef.current = null;
      agentFitRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!hostRef.current) return;
    setExited(false);
    setObservation({ state: "unknown", at: Date.now() });
    setSubmission("");
    const host = hostRef.current;
    const term = new Terminal({
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      fontSize: 12,
      theme: {
        background: "#0a0a0c",
        foreground: "#d4d4d8",
        cursor: "#7c8cff",
        selectionBackground: "#2a2a3a",
      },
      cursorBlink: true,
      scrollback: 5000,
      cols: 80,
      rows: 24,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    // Clickable https URLs (WebLinksAddon) + workspace path tokens (own provider).
    attachTerminalLinkHandlers(term);
    term.open(host);
    termRef.current = term;
    const selectionSub = term.onSelectionChange(() => {
      if (agentViewRef.current) return;
      const text = readLiveTerminalSelection(term);
      setSelection(text);
      setSelectionStyle(text.trim() ? terminalSelectionAnchor(host) : null);
    });

    let disposed = false;
    let layoutWaitRo: ResizeObserver | null = null;

    const markExited = (msg?: string) => {
      if (disposed) return;
      if (msg) {
        try { term.write(msg); } catch { /* ignore */ }
      }
      setExited(true);
    };

    const fitSafe = () => {
      try { fit.fit(); } catch { /* ignore */ }
      return safePtyDims(term.cols, term.rows);
    };

    let lastOffset = 0;
    let attachment = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    const attachStream = (sid: string) => {
      const generation = ++attachment;
      const current = () => !disposed && idRef.current === sid && generation === attachment;
      let sawOutput = false;
      let sawExit = false;
      cancelRef.current = stream(
        terminalStreamPath(sid, lastOffset),
        (raw: unknown) => {
          if (!terminalEventIsCurrent(raw, sid, current())) return;
          const ev = decodeTerminalStreamEvent(raw);
          if (ev.kind === "observation") setObservation({ state: "unknown", at: Date.now() });
          if (ev.kind === "gap") {
            lastOffset = ev.offset;
            term.write(ev.reason === "trimmed"
              ? "\r\n[earlier terminal output trimmed]\r\n"
              : "\r\n[terminal cursor reset; replaying retained output]\r\n");
          } else if (ev.offset !== undefined && ev.offset > lastOffset) lastOffset = ev.offset;
          if (ev.kind === "data") {
            sawOutput = true;
            setObservation({ state: "active_output", at: Date.now() });
            try { term.write(_b64ToBytes(ev.b64)); } catch { /* ignore */ }
          } else if (ev.kind === "process_exit" || ev.kind === "legacy_exit") {
            if (sawExit) return;
            sawExit = true;
            setObservation({ state: "exited", at: Date.now() });
            term.write("\r\n\x1b[90m[process exited -- press Restart]\x1b[0m\r\n");
            idRef.current = "";
            markExited();
          } else if (ev.kind === "missing_session") {
            if (sawExit) return;
            setObservation({ state: "stale", at: Date.now() });
            const action = terminalMissingSessionAction(autoRecoveredRef.current);
            if (action === "auto_recover") {
              autoRecoveredRef.current = true;
              idRef.current = "";
              postJSON("/api/terminal/kill", { id: sid });
              setRestartNonce((n) => n + 1);
            } else {
              sawExit = true;
              idRef.current = "";
              markExited(`\r\n\x1b[90m[session unavailable${terminalNotice(ev.error) ? `: ${terminalNotice(ev.error)}` : ""} -- press Restart]\x1b[0m\r\n`);
            }
          } else if (ev.kind === "stream_error") {
            if (sawExit) return;
            setObservation({ state: "stale", at: Date.now() });
          }
        },
        // A stream drop does not prove the process exited.
        () => {
          if (!current()) return;
          const action = terminalBareOnDoneAction({
            disposed,
            sawExit,
            hasSession: Boolean(idRef.current),
            sawOutput,
            autoRecovered: autoRecoveredRef.current,
          });
          if (action === "noop") return;
          if (action === "reattach") {
            const liveId = idRef.current;
            setObservation({ state: "stale", at: Date.now() });
            clearTimeout(reconnectTimer);
            if (liveId) reconnectTimer = setTimeout(() => { if (current()) attachStream(liveId); }, 1000);
            return;
          }
          markExited();
        },
        // onError: backend gone / stream broke -- surface a restartable state
        () => {
          if (!current()) return;
          setObservation({ state: "stale", at: Date.now() });
          clearTimeout(reconnectTimer);
          reconnectTimer = setTimeout(() => { if (current()) attachStream(sid); }, 1000);
        }
      );
    };

    (async () => {
      try {
        // Wait for a real host box before create — FitAddon on a 0-size dock
        // yields 0x0, which Windows ConPTY rejects (empty EXITED pane).
        if (!hostHasLayout(host)) {
          await new Promise<void>((resolve) => {
            const timeout = window.setTimeout(() => {
              layoutWaitRo?.disconnect();
              layoutWaitRo = null;
              resolve();
            }, 2500);
            layoutWaitRo = new ResizeObserver(() => {
              if (!hostHasLayout(host)) return;
              window.clearTimeout(timeout);
              layoutWaitRo?.disconnect();
              layoutWaitRo = null;
              resolve();
            });
            layoutWaitRo.observe(host);
          });
        }
        if (disposed) return;

        const dims = fitSafe();
        const res = await postJSON<{ id: string }>("/api/terminal/create", dims);
        if (disposed) { postJSON("/api/terminal/kill", { id: res.id }); return; }
        idRef.current = res.id;

        // keystrokes -> backend
        term.onData((data) => {
          if (idRef.current) void writeInput(idRef.current, data);
        });
        // resize -> backend (never send 0x0 — ConPTY rejects it)
        term.onResize(({ cols, rows }) => {
          if (!idRef.current) return;
          const next = safePtyDims(cols, rows);
          postJSON("/api/terminal/resize", { id: idRef.current, cols: next.cols, rows: next.rows });
        });

        attachStream(res.id);
      } catch (e) {
        if (disposed) return;
        setObservation({ state: "stale", at: Date.now() });
        const detail = e instanceof Error && e.message ? ` (${e.message})` : "";
        markExited(
          `\r\n\x1b[31mFailed to start terminal${detail} -- press Restart.\x1b[0m\r\n`
        );
      }
    })();

    // fit on container resize (clamp so a collapsed frame cannot push 0x0)
    const ro = new ResizeObserver(() => {
      if (disposed) return;
      const next = fitSafe();
      if (idRef.current) {
        postJSON("/api/terminal/resize", { id: idRef.current, cols: next.cols, rows: next.rows });
      }
    });
    ro.observe(host);

    return () => {
      disposed = true;
      clearTimeout(reconnectTimer);
      layoutWaitRo?.disconnect();
      ro.disconnect();
      if (cancelRef.current) cancelRef.current();
      if (idRef.current) postJSON("/api/terminal/kill", { id: idRef.current });
      idRef.current = "";
      try { selectionSub.dispose(); } catch { /* ignore */ }
      term.dispose();
      clearSelectionUi();
    };
  }, [restartNonce]);

  // Cmd/Ctrl+L adds the live xterm selection to the composer. Only swallow
  // the key when there is text -- otherwise App.tsx still focuses the input.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!isAddSelectionShortcut(e, isMacNavigator())) return;
      const agent = agentViewRef.current;
      const term = agent ? agentTermRef.current : termRef.current;
      if (!readLiveTerminalSelection(term).trim()) return;
      e.preventDefault();
      e.stopPropagation();
      addSelectionToChat(term, agent ? "agent" : "term");
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  const agentLabel = agentView
    ? (agentView.command.length > 64
      ? `${agentView.command.slice(0, 61)}...`
      : agentView.command)
    : "";

  return (
    <div className="h-full flex flex-col bg-transparent">
      <div className="px-3 py-2 border-b border-edge flex items-center justify-between shrink-0 gap-2">
        {agentView ? (
          <>
            <span
              className="text-ui-10 text-faint font-medium truncate min-w-0 font-mono"
              title={agentView.command}
            >
              {agentLabel}
            </span>
            <button
              type="button"
              onClick={showInteractiveShell}
              title="Return to the interactive shell (keeps this command history)"
              className="shrink-0 text-ui-10 px-1.5 py-0.5 rounded border text-faint border-edge2 hover:text-muted hover:bg-panel2/60 transition-colors"
            >
              Interactive shell
            </button>
          </>
        ) : (
          <>
            <span className="text-ui-10 uppercase tracking-wider text-faint font-medium">
              Terminal -- {terminalObservationLabel(observation.state, observation.at, now)}
              {submission && <span
                title="Input receipts confirm only bytes accepted by the PTY, not command execution or completion."
                aria-label={`${submission}. Input receipts confirm only bytes accepted by the PTY, not command execution or completion.`}
              >{` / ${submission}`}</span>}
            </span>
            <button
              onClick={restart}
              title="Restart terminal (kills the current shell and starts a fresh one)"
              className={`flex items-center gap-1 text-ui-10 px-1.5 py-0.5 rounded border transition-colors ${
                exited
                  ? "bg-accent/15 text-accent border-accent/30 hover:bg-accent/25"
                  : "text-faint border-edge2 hover:text-muted hover:bg-panel2/60"
              }`}
            >
              <RotateCw size={11} /> Restart
            </button>
          </>
        )}
      </div>
      <div className="relative flex-1 min-h-0" data-terminal="">
        <div
          ref={hostRef}
          className={`absolute inset-0 p-1.5 overflow-hidden ${agentView ? "invisible pointer-events-none" : ""}`}
          aria-hidden={Boolean(agentView)}
        />
        <div
          ref={agentHostRef}
          className={`absolute inset-0 p-1.5 overflow-hidden ${agentView ? "" : "invisible pointer-events-none"}`}
          aria-hidden={!agentView}
          data-testid="agent-terminal-mirror"
        />
        {selection.trim() ? (
          <button
            type="button"
            data-testid="terminal-add-to-chat"
            title={`Add selection to chat (${addToChatShortcutHint(isMacNavigator())})`}
            className="absolute z-10 text-ui-10 px-1.5 py-0.5 rounded border text-faint border-edge2 bg-[#0f1113] hover:text-muted hover:bg-panel2/80 transition-colors"
            style={selectionStyle ?? { right: 12, top: 8 }}
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              addSelectionToChat(
                agentView ? agentTermRef.current : termRef.current,
                agentView ? "agent" : "term",
              );
            }}
          >
            Add to chat
            <span className="ml-1 opacity-60">
              {addToChatShortcutHint(isMacNavigator())}
            </span>
          </button>
        ) : null}
      </div>
    </div>
  );
}

// decode a base64 string to a Uint8Array for xterm.write (preserves raw bytes/ANSI)
function _b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr;
}
