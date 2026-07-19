"use strict";

const net = require("node:net");

function parseHttpUrl(urlString) {
  if (typeof urlString !== "string" || !urlString) return null;
  try {
    const u = new URL(urlString);
    if (u.protocol !== "http:") return null;
    return u;
  } catch {
    return null;
  }
}

/**
 * Exact-origin authenticated `/api/` request matcher.
 *
 * Security goals:
 * - No substring matching (URL parsing only)
 * - Must match active backend `host+port` (loopback alias only if allowed)
 * - Must match request `pathname` prefix `/api/` structurally
 */
function isExactOriginAuthenticatedApiRequest(
  urlString,
  { activePort, allowedLoopbackHostnames, apiPathPrefix = "/api/" }
) {
  const u = parseHttpUrl(urlString);
  if (!u) return false;
  if (u.port !== String(activePort)) return false;
  if (!allowedLoopbackHostnames || !allowedLoopbackHostnames.has(u.hostname)) return false;
  if (!u.pathname || !u.pathname.startsWith(apiPathPrefix)) return false;
  return true;
}

function maybeInjectXHarnessTokenHeader({
  urlString,
  requestHeaders,
  harnessToken,
  activePort,
  allowedLoopbackHostnames,
}) {
  const headers = requestHeaders && typeof requestHeaders === "object" ? requestHeaders : {};
  const shouldInject = isExactOriginAuthenticatedApiRequest(urlString, {
    activePort,
    allowedLoopbackHostnames,
  });
  if (!shouldInject) return false;
  headers["X-Harness-Token"] = harnessToken;
  return true;
}

function probeTcpHost(host, port, timeoutMs) {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port, timeout: timeoutMs }, () => {
      socket.end();
      resolve(true);
    });
    socket.once("error", () => resolve(false));
    socket.once("timeout", () => {
      socket.destroy();
      resolve(false);
    });
  });
}

/**
 * Determines which loopback aliases are actually reachable for the active backend
 * `host:port` without assuming DNS behavior or IPv4/IPv6 binding.
 *
 * Unit tests avoid network dependencies by calling the pure matcher directly.
 */
async function probeActiveLoopbackAliasesForPort(
  port,
  { timeoutMs = 250 } = {}
) {
  // Always allow the canonical host we bind the backend to.
  const allowed = new Set(["127.0.0.1"]);
  const candidates = ["localhost", "::1"];
  await Promise.all(
    candidates.map(async (host) => {
      const ok = await probeTcpHost(host, port, timeoutMs);
      if (ok) allowed.add(host);
    })
  );
  return allowed;
}

module.exports = {
  isExactOriginAuthenticatedApiRequest,
  maybeInjectXHarnessTokenHeader,
  probeActiveLoopbackAliasesForPort,
};

