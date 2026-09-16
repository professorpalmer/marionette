const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { isAllowedBrowserUrl, isAllowedExternalUrl, hardenWebPreferences } = require("./browser-url.cjs");

describe("isAllowedBrowserUrl", () => {
  it("allows http(s)", () => {
    assert.equal(isAllowedBrowserUrl("https://example.com/x"), true);
    assert.equal(isAllowedBrowserUrl("http://127.0.0.1:8000"), true);
  });

  it("rejects file and custom schemes", () => {
    assert.equal(isAllowedBrowserUrl("file:///etc/passwd"), false);
    assert.equal(isAllowedBrowserUrl("marionette://wiki-connect"), false);
    assert.equal(isAllowedBrowserUrl("javascript:alert(1)"), false);
    assert.equal(isAllowedBrowserUrl(""), false);
  });

  it("optionally allows about:blank for popout", () => {
    assert.equal(isAllowedBrowserUrl("about:blank"), false);
    assert.equal(isAllowedBrowserUrl("about:blank", { allowBlank: true }), true);
  });

  it("allows mailto only on the external-open path", () => {
    assert.equal(isAllowedExternalUrl("mailto:ops@example.com"), true);
    assert.equal(isAllowedExternalUrl("https://example.com"), true);
    assert.equal(isAllowedExternalUrl("file:///etc/passwd"), false);
    assert.equal(isAllowedBrowserUrl("mailto:ops@example.com"), false);
  });

  it("hardens new-window prefs without stripping preload", () => {
    const prefs = hardenWebPreferences({ preload: "/trusted/preload.cjs" });
    assert.equal(prefs.sandbox, true);
    assert.equal(prefs.contextIsolation, true);
    assert.equal(prefs.nodeIntegration, false);
    assert.equal(prefs.preload, "/trusted/preload.cjs");
  });
});
