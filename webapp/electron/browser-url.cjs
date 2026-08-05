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

module.exports = { isAllowedBrowserUrl };
