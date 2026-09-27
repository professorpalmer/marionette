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
// Errors expose a status, an allowlisted code and a static message. Response
// text and request details never cross IPC into renderer state or logs.

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

const INPUT_ERROR_CODES = new Set([
  "input_already_attempted", "input_archive_unavailable", "input_attachment_corrupt",
  "input_attachment_invalid", "input_attachment_limit", "input_attachment_unavailable", "input_attachment_unknown",
  "input_commit_uncertain", "input_corrupt", "input_delivery_uncertain",
  "input_document_missing", "input_evidence_missing", "input_evidence_unreadable",
  "input_handoff_conflict", "input_held", "input_id_conflict", "input_invalid",
  "input_lock_failed", "input_owner_invalid", "input_owner_required",
  "input_publication_conflict", "input_read_failed", "input_restore_conflict",
  "input_restore_required", "input_retry_conflict", "input_storage_unavailable",
  "input_terminal", "input_transcript_unreadable", "input_transition_invalid", "input_unknown",
  "input_archive_stale", "input_stop_uncertain", "input_stopped", "input_session_changed", "input_stash_expired",
]);

/** Safe input_* vocabulary token: never forward free-form / secret-bearing codes. */
function isSafeInputErrorCode(code) {
  return typeof code === "string" && /^input_[a-z0-9_]+$/.test(code);
}

function sanitizedInputError(status, text) {
  try {
    const body = JSON.parse(text);
    const code = body && body.code;
    // Preserve allowlisted and other safe input_* codes; never forward bodies.
    // Endpoint rotation is the only 409 that invalidates the renderer's pin.
    if (code === "endpoint_mismatch" || code === "boot_mismatch") {
      return { status, code, message: "Backend connection changed; reconnecting." };
    }
    if (isSafeInputErrorCode(code)) {
      return {
        status,
        code,
        message: "Input delivery could not be confirmed. Keep your draft and review it before sending again.",
      };
    }
  } catch { /* malformed errors retain the HTTP classification */ }
  return sanitizedStreamHttpError(status);
}

/**
 * Wire a backend SSE http.IncomingMessage to exactly one terminal callback.
 *
 * - non-2xx status: onError(sanitized), never onDone. Bounded input error JSON
 *   preserves only known codes; response text is never forwarded.
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
    if ([400, 409, 413, 422, 503].includes(status)) {
      let body = "";
      let bytes = 0;
      const complete = (payload) => {
        clearTimeout(timer);
        finishError(payload);
        try { res.destroy(); } catch { /* already closed */ }
      };
      const timer = setTimeout(() => complete(sanitizedStreamHttpError(status)), 2000);
      timer.unref?.();
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        if (settled) return;
        bytes += Buffer.byteLength(chunk);
        if (bytes > 4096) {
          body = "";
          complete(sanitizedStreamHttpError(status));
          return;
        }
        body += chunk;
      });
      res.on("end", () => complete(sanitizedInputError(status, body)));
      res.on("error", () => complete(sanitizedStreamHttpError(status)));
      res.on("aborted", () => complete(sanitizedStreamHttpError(status)));
      return;
    }
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
