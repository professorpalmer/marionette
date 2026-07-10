"use strict";

/**
 * Pure helper: decide whether a backend.json marker should replace the
 * in-memory port used by Electron IPC. Used when a request hits ECONNREFUSED
 * against a stale port while another process (or a respawn) already wrote a
 * new healthy marker.
 *
 * @param {string|null|undefined} raw JSON text from backend.json (or null)
 * @param {number|null|undefined} currentPort in-memory port Electron is using
 * @returns {{ adopt: boolean, port: number|null }}
 */
function decideBackendPortRefresh(raw, currentPort) {
  if (!raw) return { adopt: false, port: null };
  let m;
  try {
    m = JSON.parse(raw);
  } catch {
    return { adopt: false, port: null };
  }
  if (!m || m.port == null) return { adopt: false, port: null };
  const port = Number(m.port);
  if (!Number.isFinite(port) || port <= 0) return { adopt: false, port: null };
  const cur = currentPort == null ? null : Number(currentPort);
  if (cur != null && Number.isFinite(cur) && port === cur) {
    return { adopt: false, port };
  }
  return { adopt: true, port };
}

module.exports = { decideBackendPortRefresh };
