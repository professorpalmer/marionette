"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  isExactOriginAuthenticatedApiRequest,
  maybeInjectXHarnessTokenHeader,
} = require("./resource-auth.cjs");

function makeMatcherArgs({ activePort, allowedHostnames }) {
  return {
    activePort,
    allowedLoopbackHostnames: new Set(allowedHostnames),
    apiPathPrefix: "/api/",
  };
}

describe("resource-auth: exact origin + exact /api/ pathname", () => {
  it("injects only for the active host+port and /api/ pathname", () => {
    const matcherArgs = makeMatcherArgs({
      activePort: 8799,
      allowedHostnames: ["127.0.0.1"],
    });

    assert.equal(
      isExactOriginAuthenticatedApiRequest("http://127.0.0.1:8799/api/upload", matcherArgs),
      true,
    );
    assert.equal(
      isExactOriginAuthenticatedApiRequest("http://127.0.0.1:8799/api/", matcherArgs),
      true,
    );
  });

  it("rejects wrong loopback port even for loopback hostname", () => {
    const matcherArgs = makeMatcherArgs({
      activePort: 8799,
      allowedHostnames: ["127.0.0.1"],
    });
    assert.equal(
      isExactOriginAuthenticatedApiRequest("http://127.0.0.1:8800/api/upload", matcherArgs),
      false,
    );
  });

  it("rejects external URLs (even if they contain /api/ in path)", () => {
    const matcherArgs = makeMatcherArgs({
      activePort: 8799,
      allowedHostnames: ["127.0.0.1"],
    });
    assert.equal(
      isExactOriginAuthenticatedApiRequest("http://example.com:8799/api/upload", matcherArgs),
      false,
    );
  });

  it("rejects query/path trickery: only pathname prefix counts", () => {
    const matcherArgs = makeMatcherArgs({
      activePort: 8799,
      allowedHostnames: ["127.0.0.1"],
    });
    // /api/ appears only in query.
    assert.equal(
      isExactOriginAuthenticatedApiRequest(
        "http://127.0.0.1:8799/notapi?x=/api/upload",
        matcherArgs,
      ),
      false,
    );
    // /api without trailing slash is not /api/ prefix.
    assert.equal(
      isExactOriginAuthenticatedApiRequest("http://127.0.0.1:8799/api?x=1", matcherArgs),
      false,
    );
    // Encoded slash in the path should not become a real `/api/` prefix.
    assert.equal(
      isExactOriginAuthenticatedApiRequest("http://127.0.0.1:8799/api%2Fupload", matcherArgs),
      false,
    );
  });

  it("allows supported aliases only when the alias is in the allowed set", () => {
    // If probing decided `localhost` did NOT resolve to this backend endpoint,
    // we must not inject into `localhost`.
    const matcherArgs = makeMatcherArgs({
      activePort: 8799,
      allowedHostnames: ["127.0.0.1"], // localhost intentionally excluded
    });
    assert.equal(
      isExactOriginAuthenticatedApiRequest("http://localhost:8799/api/upload", matcherArgs),
      false,
    );
  });
});

describe("resource-auth: restart updates token + active port usage", () => {
  it("uses the current token only for requests matching the current backend port", () => {
    const requestHeaders = {};

    const injectedOld = maybeInjectXHarnessTokenHeader({
      urlString: "http://127.0.0.1:8799/api/upload",
      requestHeaders,
      harnessToken: "old-token",
      activePort: 8799,
      allowedLoopbackHostnames: new Set(["127.0.0.1"]),
    });

    assert.equal(injectedOld, true);
    assert.equal(requestHeaders["X-Harness-Token"], "old-token");

    // Same URL but backend moved to a new port: no injection, token unchanged.
    const requestHeadersAfter = { "X-Harness-Token": "keep-me" };
    const injectedNewPort = maybeInjectXHarnessTokenHeader({
      urlString: "http://127.0.0.1:8800/api/upload",
      requestHeaders: requestHeadersAfter,
      harnessToken: "new-token",
      activePort: 8799, // old active port
      allowedLoopbackHostnames: new Set(["127.0.0.1"]),
    });
    assert.equal(injectedNewPort, false);
    assert.equal(requestHeadersAfter["X-Harness-Token"], "keep-me");

    // Now match the new active port; injection must use the latest token.
    const requestHeadersNew = {};
    const injectedLatest = maybeInjectXHarnessTokenHeader({
      urlString: "http://127.0.0.1:8800/api/upload",
      requestHeaders: requestHeadersNew,
      harnessToken: "new-token",
      activePort: 8800,
      allowedLoopbackHostnames: new Set(["127.0.0.1"]),
    });
    assert.equal(injectedLatest, true);
    assert.equal(requestHeadersNew["X-Harness-Token"], "new-token");
  });
});

