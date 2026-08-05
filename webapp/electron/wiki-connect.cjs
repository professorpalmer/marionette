/**
 * marionette://wiki-connect parsing + trust checks (shared by main + unit tests).
 *
 * Deep links can be opened by other apps/pages once the protocol is registered.
 * Never accept an arbitrary api_base: only loopback and portablellm.wiki hosts.
 */

function parseWikiConnectDeepLink(raw) {
  if (!raw || typeof raw !== "string") return null;
  const text = raw.trim();
  if (!text.toLowerCase().startsWith("marionette://wiki-connect")) return null;
  try {
    // URL() needs a parseable host; normalize scheme for WHATWG parser.
    const normalized = text.replace(/^marionette:\/\//i, "https://marionette/");
    const u = new URL(normalized);
    const personalUrl = u.searchParams.get("url") || "";
    const apiBase = u.searchParams.get("api_base") || "";
    const token = u.searchParams.get("token") || u.searchParams.get("t") || "";
    if (personalUrl) return { api_base: personalUrl, owner_token: undefined };
    if (apiBase) return { api_base: apiBase, owner_token: token || undefined };
  } catch {
    return null;
  }
  return null;
}

function isLoopbackWikiConnectUrl(url) {
  if (typeof url !== "string") return false;
  if (!/\/api\/wiki\/connect(\?|$|#)/i.test(url)) return false;
  if (!/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])/i.test(url)) return false;
  try {
    const u = new URL(url);
    const nonce = u.searchParams.get("nonce") || "";
    return !!nonce;
  } catch {
    return false;
  }
}

/** Hosts allowed for deep-link wiki connect (fail closed). */
const TRUSTED_WIKI_CONNECT_HOSTS = new Set([
  "127.0.0.1",
  "localhost",
  "::1",
  "api.portablellm.wiki",
  "portablellm.wiki",
  "www.portablellm.wiki",
]);

/**
 * True when api_base is loopback or a portablellm.wiki host.
 * Personal LLM URLs (portablellm.wiki/.../llm?t=...) are trusted so the
 * backend can normalize them; arbitrary remotes are rejected.
 */
function isTrustedWikiConnectApiBase(apiBase) {
  if (!apiBase || typeof apiBase !== "string") return false;
  try {
    const u = new URL(apiBase.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    const host = (u.hostname || "").toLowerCase();
    return TRUSTED_WIKI_CONNECT_HOSTS.has(host);
  } catch {
    return false;
  }
}

module.exports = {
  parseWikiConnectDeepLink,
  isLoopbackWikiConnectUrl,
  isTrustedWikiConnectApiBase,
  TRUSTED_WIKI_CONNECT_HOSTS,
};
