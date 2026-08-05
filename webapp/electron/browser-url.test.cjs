const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { isAllowedBrowserUrl } = require("./browser-url.cjs");

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
});
