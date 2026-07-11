import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { RotateCw } from "lucide-react";
import { postJSON, stream, withToken } from "../lib/transport";
import { isExternalUrl, looksLikeFilePath, openAgentFile, openAgentUrl } from "../lib/agentLinks";

// Built-in terminal: xterm.js front-end over the harness PTY backend.
// create -> SSE stream output (base64 frames) -> POST keystrokes -> resize -> kill.
// A restart counter lets the user relaunch a dead/stuck shell without reloading
// the app (the previous session is killed cleanly first).
export default function TerminalPane() {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const idRef = useRef<string>("");
  const cancelRef = useRef<null | (() => void)>(null);
  // Bumping this re-runs the effect: cleanly tears down the old PTY + xterm and
  // spins up a fresh one. Drives the Restart button and exit auto-recovery.
  const [restartNonce, setRestartNonce] = useState(0);
  const [exited, setExited] = useState(false);

  const restart = () => {
    setExited(false);
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
    term.open(hostRef.current);
    try { fit.fit(); } catch { /* ignore */ }
    termRef.current = term;

    let disposed = false;

    (async () => {
      try {
        const res = await postJSON<{ id: string }>("/api/terminal/create", {
          cols: term.cols, rows: term.rows,
        });
        if (disposed) { postJSON("/api/terminal/kill", { id: res.id }); return; }
        idRef.current = res.id;

        // keystrokes -> backend
        term.onData((data) => {
          if (idRef.current) postJSON("/api/terminal/write", { id: idRef.current, data });
        });
        // resize -> backend
        term.onResize(({ cols, rows }) => {
          if (idRef.current) postJSON("/api/terminal/resize", { id: idRef.current, cols, rows });
        });

        // stream output
        cancelRef.current = stream(
          `/api/terminal/stream?id=${res.id}`,
          (ev: any) => {
            if (ev.kind === "data" && ev.b64) {
              try { term.write(_b64ToBytes(ev.b64)); } catch { /* ignore */ }
            } else if (ev.kind === "exit") {
              term.write("\r\n\x1b[90m[process exited -- press Restart]\x1b[0m\r\n");
              idRef.current = "";  // session is dead; stop sending keystrokes to it
              if (!disposed) setExited(true);
            }
          },
          // onDone: SSE closed (shell exited / backend closed the stream)
          () => { if (!disposed) setExited(true); },
          // onError: backend gone / stream broke -- surface a restartable state
          () => { if (!disposed) setExited(true); }
        );
      } catch (e) {
        term.write("\r\n\x1b[31mFailed to start terminal -- press Restart.\x1b[0m\r\n");
        if (!disposed) setExited(true);
      }
    })();

    // fit on container resize
    const ro = new ResizeObserver(() => { try { fit.fit(); } catch { /* ignore */ } });
    ro.observe(hostRef.current);

    return () => {
      disposed = true;
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

void withToken;
