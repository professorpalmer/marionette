/**
 * Unit tests for marionette://wiki-connect parsing + trust checks.
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  parseWikiConnectDeepLink,
  isLoopbackWikiConnectUrl,
  isTrustedWikiConnectApiBase,
} = require("./wiki-connect.cjs");

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

describe("isTrustedWikiConnectApiBase", () => {
  it("allows portablellm and loopback", () => {
    assert.equal(
      isTrustedWikiConnectApiBase("https://api.portablellm.wiki/t/acme"),
      true,
    );
    assert.equal(
      isTrustedWikiConnectApiBase("https://portablellm.wiki/acme/llm?t=x"),
      true,
    );
    assert.equal(isTrustedWikiConnectApiBase("http://127.0.0.1:8000"), true);
    assert.equal(isTrustedWikiConnectApiBase("http://localhost:8000/wiki"), true);
  });

  it("rejects arbitrary remote hosts (deep-link exfil surface)", () => {
    assert.equal(isTrustedWikiConnectApiBase("https://evil.example/wiki"), false);
    assert.equal(isTrustedWikiConnectApiBase("https://api.portablellm.wiki.evil.com/t/x"), false);
    assert.equal(isTrustedWikiConnectApiBase("ftp://portablellm.wiki/x"), false);
    assert.equal(isTrustedWikiConnectApiBase(""), false);
  });
});
