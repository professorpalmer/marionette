/**
 * Pins the renderer CSP that vite.config.ts injects into the production
 * index.html. The policy rides in the document (not a response header) because
 * the packaged shell loads dist/index.html over file://, where Electron's
 * webRequest never fires.
 *
 * These assertions exist because a CSP is only a guard while it is actually
 * present and actually strict -- and because several directives here are
 * deliberately loosened for shipped features (see src/lib/csp.ts). If someone
 * deletes a directive or weakens script-src, this fails instead of silently
 * shipping a no-op policy.
 */
import { describe, expect, it } from "vitest";
import { CSP_DIRECTIVES, cspMetaTag, cspPolicy } from "../lib/csp";

describe("renderer Content-Security-Policy", () => {
  it("keeps script-src strict: no inline, no eval, no remote scripts", () => {
    const scriptSrc = CSP_DIRECTIVES["script-src"];
    expect(scriptSrc).toContain("'self'");
    // The prepaint snippet is an external file precisely so these stay out.
    expect(scriptSrc).not.toContain("'unsafe-inline'");
    expect(scriptSrc).not.toContain("'unsafe-eval'");
    expect(scriptSrc.some((s) => s.startsWith("http"))).toBe(false);
    expect(scriptSrc).not.toContain("*");
  });

  it("blocks plugins and base-tag hijacking", () => {
    expect(CSP_DIRECTIVES["object-src"]).toEqual(["'none'"]);
    expect(CSP_DIRECTIVES["base-uri"]).toEqual(["'none'"]);
  });

  it("no CSP source is one Chromium rejects outright", () => {
    // Bracketed IPv6 plus a wildcard port is invalid CSP grammar: Chromium drops
    // it with a console error. Caught in a real file:// load, so pin it here.
    const all = Object.values(CSP_DIRECTIVES).flat();
    for (const source of all) {
      expect(source).not.toMatch(/^[a-z]+:\/\/\[/);
    }
  });

  it("does not use a blanket wildcard default", () => {
    expect(CSP_DIRECTIVES["default-src"]).not.toContain("*");
  });

  it("allows only loopback backend traffic on connect-src", () => {
    const connectSrc = CSP_DIRECTIVES["connect-src"];
    const http = connectSrc.filter((s) => s.startsWith("http"));
    expect(http.length).toBeGreaterThan(0);
    for (const origin of http) {
      // Every http origin must be loopback: the renderer talks to the local
      // harness and nothing else.
      expect(origin).toMatch(/^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):\*$/);
    }
    // ws: is required for the dev-server HMR socket.
    expect(connectSrc.some((s) => s.startsWith("ws://"))).toBe(true);
  });

  it("keeps the Browser pane usable: frame-src stays open", () => {
    // The Browser tab renders arbitrary user-navigated sites in a <webview>;
    // those guests are isolated by hardenWebPreferences + a sandboxed preload,
    // not by this directive. Closing frame-src would break the product.
    expect(CSP_DIRECTIVES["frame-src"]).toEqual(["*"]);
  });

  it("keeps the app's real <form> submits working", () => {
    // ScheduleEditor / RegistryWizard / MemoryPane / OnboardingOverlay /
    // CheckpointsPane all submit forms -- form-action 'none' would break them.
    expect(CSP_DIRECTIVES["form-action"]).toEqual(["'self'"]);
  });

  it("serializes into a parseable meta tag", () => {
    const tag = cspMetaTag();
    expect(tag.startsWith("<meta http-equiv=\"Content-Security-Policy\"")).toBe(true);
    expect(tag.endsWith("/>")).toBe(true);
    const policy = cspPolicy();
    // Directives are '; '-joined name + space-separated sources.
    for (const [directive, sources] of Object.entries(CSP_DIRECTIVES)) {
      expect(policy).toContain(`${directive} ${sources.join(" ")}`);
    }
    expect(policy).not.toContain(";;");
  });
});
