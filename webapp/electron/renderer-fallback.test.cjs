"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  normalizeRendererOrigin,
  configuredDevOriginMatches,
  shouldAbandonConfiguredDevServer,
  shouldRetryFailedRendererLoad,
  resolveClassicDevRendererSource,
  createDevServerFallbackLatch,
} = require("./renderer-fallback.cjs");

const DEV = "http://127.0.0.1:5273";

function fail(overrides) {
  return {
    isMainFrame: true,
    errorCode: -102,
    validatedURL: "http://127.0.0.1:5273/",
    configuredDevServerUrl: DEV,
    ...overrides,
  };
}

test("matching -102 main frame abandons", () => {
  assert.equal(shouldAbandonConfiguredDevServer(fail()), true);
  assert.equal(
    shouldRetryFailedRendererLoad({ ...fail(), abandoned: false }),
    false,
  );
});

test("trailing slash and path normalize to the configured origin", () => {
  assert.equal(normalizeRendererOrigin(DEV), "http://127.0.0.1:5273");
  assert.equal(normalizeRendererOrigin("http://127.0.0.1:5273/"), "http://127.0.0.1:5273");
  assert.equal(configuredDevOriginMatches(DEV, "http://127.0.0.1:5273/"), true);
  assert.equal(configuredDevOriginMatches(DEV, "http://127.0.0.1:5273/index.html"), true);
  assert.equal(configuredDevOriginMatches(`${DEV}/`, "http://127.0.0.1:5273/foo/bar"), true);
  assert.equal(
    shouldAbandonConfiguredDevServer(fail({ validatedURL: "http://127.0.0.1:5273/app/" })),
    true,
  );
});

test("subframe and aborted -3 failures are ignored", () => {
  assert.equal(shouldAbandonConfiguredDevServer(fail({ isMainFrame: false })), false);
  assert.equal(shouldAbandonConfiguredDevServer(fail({ errorCode: -3 })), false);
  assert.equal(
    shouldRetryFailedRendererLoad({ ...fail({ isMainFrame: false }), abandoned: false }),
    false,
  );
  assert.equal(
    shouldRetryFailedRendererLoad({ ...fail({ errorCode: -3 }), abandoned: false }),
    false,
  );
});

test("other port, scheme, unrelated, or invalid URL does not abandon", () => {
  assert.equal(
    shouldAbandonConfiguredDevServer(fail({ validatedURL: "http://127.0.0.1:5274/" })),
    false,
  );
  assert.equal(
    shouldAbandonConfiguredDevServer(fail({ validatedURL: "https://127.0.0.1:5273/" })),
    false,
  );
  assert.equal(
    shouldAbandonConfiguredDevServer(fail({ validatedURL: "http://example.com/" })),
    false,
  );
  assert.equal(
    shouldAbandonConfiguredDevServer(fail({ validatedURL: "not a url" })),
    false,
  );
  assert.equal(configuredDevOriginMatches(DEV, "http://127.0.0.1:5274"), false);
  assert.equal(configuredDevOriginMatches(DEV, "https://127.0.0.1:5273"), false);
});

test("empty configured URL is not abandoned", () => {
  assert.equal(shouldAbandonConfiguredDevServer(fail({ configuredDevServerUrl: "" })), false);
  assert.equal(shouldAbandonConfiguredDevServer(fail({ configuredDevServerUrl: "   " })), false);
  assert.equal(shouldAbandonConfiguredDevServer(fail({ configuredDevServerUrl: undefined })), false);
  assert.equal(normalizeRendererOrigin(""), null);
});

test("renderer source chooses dev before latch and dist after latch", () => {
  assert.equal(
    resolveClassicDevRendererSource({ configuredDevServerUrl: DEV, abandoned: false }),
    "dev",
  );
  assert.equal(
    resolveClassicDevRendererSource({ configuredDevServerUrl: DEV, abandoned: true }),
    "dist",
  );
  assert.equal(
    resolveClassicDevRendererSource({ configuredDevServerUrl: "", abandoned: false }),
    "dist",
  );
});

test("unrelated main-frame failures still retry while the latch is unset", () => {
  assert.equal(
    shouldRetryFailedRendererLoad({
      ...fail({ validatedURL: "http://127.0.0.1:9/" }),
      abandoned: false,
    }),
    true,
  );
});

test("latch remains sticky and does not retry dist after abandonment", () => {
  const latch = createDevServerFallbackLatch();
  assert.equal(latch.isAbandoned(), false);
  assert.equal(latch.noteFailure(fail()), true);
  assert.equal(latch.isAbandoned(), true);
  assert.equal(
    latch.noteFailure(fail({
      errorCode: -6,
      validatedURL: "file:///tmp/webapp/dist/index.html",
    })),
    false,
  );
  assert.equal(latch.isAbandoned(), true);
  assert.equal(
    resolveClassicDevRendererSource({
      configuredDevServerUrl: DEV,
      abandoned: latch.isAbandoned(),
    }),
    "dist",
  );
  assert.equal(
    shouldRetryFailedRendererLoad({
      isMainFrame: true,
      errorCode: -6,
      abandoned: true,
      validatedURL: "file:///tmp/webapp/dist/index.html",
      configuredDevServerUrl: DEV,
    }),
    false,
  );
});
