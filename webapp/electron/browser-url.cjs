/**
 * Shared http(s) allowlist for browser:openExternal and browser:popout.
 */

function isAllowedBrowserUrl(url, { allowBlank = false } = {}) {
  if (typeof url !== "string") return false;
  const target = url.trim();
  if (!target) return false;
  if (allowBlank && target === "about:blank") return true;
  return /^https?:\/\//i.test(target);
}

function isAllowedExternalUrl(url) {
  if (typeof url !== "string") return false;
  const target = url.trim();
  if (!target) return false;
  if (/^mailto:/i.test(target)) return true;
  return /^https?:\/\//i.test(target);
}

function hardenWebPreferences(prefs) {
  if (!prefs || typeof prefs !== "object") return prefs;
  prefs.sandbox = true;
  prefs.contextIsolation = true;
  prefs.nodeIntegration = false;
  prefs.nodeIntegrationInSubFrames = false;
  return prefs;
}

module.exports = { isAllowedBrowserUrl, isAllowedExternalUrl, hardenWebPreferences };
