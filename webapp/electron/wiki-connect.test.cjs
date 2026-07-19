/**
 * Unit tests for marionette://wiki-connect URL parsing (mirrors main.cjs).
 * Kept as a pure copy so we do not boot Electron in CI.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

function parseWikiConnectDeepLink(raw) {
  if (!raw || typeof raw !== "string") return null;
  const text = raw.trim();
  if (!text.toLowerCase().startsWith("marionette://wiki-connect")) return null;
  try {
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

describe("parseWikiConnectDeepLink", () => {
  it("accepts personal LLM url param", () => {
    const url =
      "marionette://wiki-connect?url=" +
      encodeURIComponent("https://portablellm.wiki/acme/llm?t=secret");
    const parsed = parseWikiConnectDeepLink(url);
    assert.equal(parsed.api_base, "https://portablellm.wiki/acme/llm?t=secret");
  });

  it("accepts api_base + token", () => {
    const parsed = parseWikiConnectDeepLink(
      "marionette://wiki-connect?api_base=https%3A%2F%2Fapi.portablellm.wiki%2Ft%2Facme&token=abc",
    );
    assert.equal(parsed.api_base, "https://api.portablellm.wiki/t/acme");
    assert.equal(parsed.owner_token, "abc");
  });

  it("rejects unrelated schemes", () => {
    assert.equal(parseWikiConnectDeepLink("https://example.com"), null);
  });
});

describe("isLoopbackWikiConnectUrl", () => {
  it("accepts loopback connect handoff", () => {
    assert.equal(
      isLoopbackWikiConnectUrl("http://127.0.0.1:8765/api/wiki/connect?nonce=abc&url=x"),
      true,
    );
    assert.equal(
      isLoopbackWikiConnectUrl("http://localhost:8765/api/wiki/connect?nonce=abc"),
      true,
    );
  });

  it("rejects non-loopback or non-connect URLs", () => {
    assert.equal(
      isLoopbackWikiConnectUrl("https://portablellm.wiki/connect/marionette"),
      false,
    );
    assert.equal(
      isLoopbackWikiConnectUrl("http://127.0.0.1:8765/api/wiki/status"),
      false,
    );
    assert.equal(
      isLoopbackWikiConnectUrl("http://127.0.0.1:8765/api/wiki/connect"),
      false,
    );
    assert.equal(isLoopbackWikiConnectUrl(""), false);
  });
});
