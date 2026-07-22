import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { RotateCw } from "lucide-react";
import { postJSON, stream } from "../lib/transport";
import { isExternalUrl, looksLikeFilePath, openAgentFile, openAgentUrl } from "../lib/agentLinks";
import { hostHasLayout, safePtyDims } from "./terminalDims";
import { terminalBareOnDoneAction } from "./terminalStreamPolicy";

// Built-in terminal: xterm.js front-end over the harness PTY backend.
// create -> SSE stream output (base64 frames) -> POST keystrokes -> resize -> kill.
// A restart counter lets the user relaunch a dead/stuck shell without reloading
// the app (the previous session is killed cleanly first).
export default function TerminalPane() {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const idRef = useRef<string>("");
  const cancelRef = useRef<null | (() => void)>(null);
  // One automatic recovery when the first SSE closes before any ConPTY bytes
  // (common React-remount / IPC race on Windows). Manual Restart always works.
  const autoRecoveredRef = useRef(false);
  // Bumping this re-runs the effect: cleanly tears down the old PTY + xterm and
  // spins up a fresh one. Drives the Restart button and exit auto-recovery.
  const [restartNonce, setRestartNonce] = useState(0);
  const [exited, setExited] = useState(false);

  const restart = () => {
    setExited(false);
    autoRecoveredRef.current = false;
    setRestartNonce((n) => n + 1);
  };

  // ActionCard "Run" injects a command into the live PTY.
  useEffect(() => {
    const onRun = (e: Event) => {
      const cmd = String((e as CustomEvent<{ command?: string }>).detail?.command || "").trim();
      if (!cmd) return;
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
      postJSON("/api/terminal/write", { id, data: cmd + "\r" });
    };
    window.addEventListener("harness-run-command", onRun as EventListener);
    return () => window.removeEventListener("harness-run-command", onRun as EventListener);
  }, []);

  useEffect(() => {
    if (!hostRef.current) return;
    setExited(false);
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
    // Clickable URLs + path-looking tokens in terminal output.
    term.loadAddon(
      new WebLinksAddon((_event, uri) => {
        if (isExternalUrl(uri)) openAgentUrl(uri);
        else if (looksLikeFilePath(uri)) openAgentFile(uri);
      })
    );
    term.open(host);
    termRef.current = term;

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

    const attachStream = (sid: string) => {
      let sawOutput = false;
      let sawExit = false;
      cancelRef.current = stream(
        `/api/terminal/stream?id=${sid}`,
        (ev: any) => {
          if (ev.kind === "data" && ev.b64) {
            sawOutput = true;
            try { term.write(_b64ToBytes(ev.b64)); } catch { /* ignore */ }
          } else if (ev.kind === "exit") {
            sawExit = true;
            term.write("\r\n\x1b[90m[process exited -- press Restart]\x1b[0m\r\n");
            idRef.current = "";  // session is dead; stop sending keystrokes to it
            markExited();
          }
        },
        // onDone: SSE closed. kind:exit already settled the pane. A bare close
        // with prior output means the transport dropped while ConPTY is still
        // alive — reattach the same id. Do NOT kill. Empty first stream still
        // gets one-shot auto-recover (kill+recreate).
        () => {
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
            if (liveId) attachStream(liveId);
            return;
          }
          if (action === "auto_recover") {
            autoRecoveredRef.current = true;
            const deadId = idRef.current;
            idRef.current = "";
            if (deadId) postJSON("/api/terminal/kill", { id: deadId });
            setRestartNonce((n) => n + 1);
            return;
          }
          // mark_exited — confirmed dead or second empty-stream failure
          if (idRef.current) {
            const deadId = idRef.current;
            idRef.current = "";
            postJSON("/api/terminal/kill", { id: deadId });
            if (!sawExit) {
              markExited("\r\n\x1b[90m[stream closed -- press Restart]\x1b[0m\r\n");
              return;
            }
          }
          markExited();
        },
        // onError: backend gone / stream broke -- surface a restartable state
        () => {
          if (disposed) return;
          idRef.current = "";
          markExited("\r\n\x1b[31m[terminal stream error -- press Restart]\x1b[0m\r\n");
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
          if (idRef.current) postJSON("/api/terminal/write", { id: idRef.current, data });
        });
        // resize -> backend (never send 0x0 — ConPTY rejects it)
        term.onResize(({ cols, rows }) => {
          if (!idRef.current) return;
          const next = safePtyDims(cols, rows);
          postJSON("/api/terminal/resize", { id: idRef.current, cols: next.cols, rows: next.rows });
        });

        attachStream(res.id);
      } catch (e) {
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
      layoutWaitRo?.disconnect();
      ro.disconnect();
      if (cancelRef.current) cancelRef.current();
      if (idRef.current) postJSON("/api/terminal/kill", { id: idRef.current });
      idRef.current = "";
      term.dispose();
    };
  }, [restartNonce]);

  return (
    <div className="h-full flex flex-col bg-[#0a0a0c]">
      <div className="px-3 py-2 border-b border-edge flex items-center justify-between shrink-0">
        <span className="text-[10px] uppercase tracking-wider text-faint font-medium">
          Terminal{exited ? " -- exited" : ""}
        </span>
        <button
          onClick={restart}
          title="Restart terminal (kills the current shell and starts a fresh one)"
          className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
            exited
              ? "bg-accent/15 text-accent border-accent/30 hover:bg-accent/25"
              : "text-faint border-edge2 hover:text-muted hover:bg-panel2/60"
          }`}
        >
          <RotateCw size={11} /> Restart
        </button>
      </div>
      <div ref={hostRef} className="flex-1 min-h-0 p-1.5 overflow-hidden" />
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
