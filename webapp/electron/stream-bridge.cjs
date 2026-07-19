"use strict";

// SSE response handling for the harness:stream IPC bridge (main.cjs).
//
// Extracted so the terminal-event contract is unit-testable without booting
// Electron. The contract that matters (v0.9.95 update-skew incident): a non-2xx
// backend response -- e.g. a 403 from the header-only auth gate answering an old
// main process that still sent `?token=` -- must surface as `:error` with a
// SANITIZED payload. It must NEVER fall through the SSE parser (which finds no
// frames) into `end` -> `:done`, which the renderer reads as a normal turn close
// and paints the misleading "[aborted] Connection closed before the turn
// finished".
//
// Sanitization rule: error payloads carry only an HTTP status and a coarse code/
// message. Never the response body, never the request path/query, and never any
// token -- these payloads cross the IPC boundary and land in renderer state/logs.

/** Structured, secret-free error payload for a non-2xx stream response. */
function sanitizedStreamHttpError(statusCode) {
  const status = Number.isInteger(statusCode) ? statusCode : 0;
  if (status === 401 || status === 403) {
    return {
      status,
      code: "auth",
      message:
        `backend rejected the stream (HTTP ${status}: authentication failed); ` +
        "the app and backend may be out of sync after an update",
    };
  }
  if (status === 404) {
    return { status, code: "not_found", message: `backend stream endpoint not found (HTTP ${status})` };
  }
  return {
    status,
    code: status >= 500 ? "backend_error" : "http_error",
    message: `backend stream request failed (HTTP ${status || "unknown"})`,
  };
}

/** Structured, secret-free error payload for a transport-level failure. */
function sanitizedStreamConnError(err) {
  const code = (err && (err.code || err.errno)) || "";
  return {
    status: null,
    code: String(code || "conn_error"),
    message: `backend stream connection failed${code ? ` (${code})` : ""}`,
  };
}

/**
 * Wire a backend SSE http.IncomingMessage to exactly one terminal callback.
 *
 * - non-2xx status: onError(sanitized) immediately; the body is drained and
 *   DISCARDED (it may echo error text) and onDone can never fire.
 * - 2xx: `data:` frames -> onEvent; a `{"kind":"done"}` frame or stream end ->
 *   onDone; a response error -> onError(sanitized).
 */
function wireStreamResponse(res, { onEvent, onDone, onError }) {
  let settled = false;
  const finishDone = () => {
    if (settled) return;
    settled = true;
    onDone();
  };
  const finishError = (payload) => {
    if (settled) return;
    settled = true;
    onError(payload);
  };

  const status = res.statusCode;
  if (!Number.isInteger(status) || status < 200 || status >= 300) {
    finishError(sanitizedStreamHttpError(status));
    // Drain so the socket can close; never parse or forward the error body.
    res.on("data", () => {});
    res.on("error", () => {});
    try { res.destroy(); } catch { /* already closed */ }
    return;
  }

  res.setEncoding("utf8");
  let buf = "";
  res.on("data", (chunk) => {
    buf += chunk;
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      const payload = line.slice(6);
      try {
        const ev = JSON.parse(payload);
        if (ev.kind === "done") {
          finishDone();
          try { res.destroy(); } catch { /* already closed */ }
          return;
        }
        if (!settled) onEvent(ev);
      } catch { /* skip malformed frame */ }
    }
  });
  res.on("end", finishDone);
  res.on("error", (e) => finishError(sanitizedStreamConnError(e)));
}

module.exports = {
  sanitizedStreamHttpError,
  sanitizedStreamConnError,
  wireStreamResponse,
};
