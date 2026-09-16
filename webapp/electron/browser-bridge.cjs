"use strict";
const http = require("node:http");
const crypto = require("node:crypto");
const ACTIONS = new Set(["tabs", "tab_activate", "navigate", "back", "snapshot", "get_text", "click", "type", "scroll", "screenshot", "input", "computer"]);

function createBrowserBridge({ dispatch, timeoutMs = 15000 }) {
  const token = crypto.randomBytes(32).toString("hex");
  const pending = new Set();
  const server = http.createServer((request, response) => {
    const abort = new AbortController();
    pending.add(abort);
    let finished = false;
    const finish = (status, body) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      pending.delete(abort);
      abort.abort();
      response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
      response.end(JSON.stringify(body));
    };
    const timer = setTimeout(() => finish(408, { ok: false, error: "Browser request timed out; inspect the page before retrying an action." }), timeoutMs);
    response.on("close", () => { clearTimeout(timer); pending.delete(abort); abort.abort(); });
    if (request.method !== "POST" || request.url !== "/browser" || request.headers.origin
        || request.headers.host !== "127.0.0.1:" + server.address().port
        || request.headers.authorization !== "Bearer " + token) {
      finish(403, { ok: false, error: "authentication failed" });
      return;
    }
    let size = 0;
    const chunks = [];
    request.on("error", () => finish(400, { ok: false, error: "invalid request" }));
    request.on("data", chunk => {
      size += chunk.length;
      if (size > 65536) finish(413, { ok: false, error: "request too large" });
      else if (!finished) chunks.push(chunk);
    });
    request.on("end", async () => {
      if (finished) return;
      try {
        const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        if (!payload || typeof payload.session_id !== "string" || !payload.session_id
            || payload.session_id.length > 256 || !ACTIONS.has(payload.action)
            || !payload.arguments || typeof payload.arguments !== "object" || Array.isArray(payload.arguments)) {
          throw new Error("invalid browser request");
        }
        const result = await dispatch(payload, abort.signal);
        if (!abort.signal.aborted) finish(200, { ok: true, result });
      } catch (error) {
        if (!abort.signal.aborted) finish(400, { ok: false, error: error.message || "browser request failed" });
      }
    });
  });
  server.headersTimeout = timeoutMs;
  server.requestTimeout = timeoutMs;
  return {
    token, server,
    listen: () => new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => { server.removeListener("error", reject); resolve(server.address().port); });
    }),
    close: () => { for (const abort of pending) abort.abort(); server.close(); server.closeAllConnections(); },
  };
}
module.exports = { createBrowserBridge };
