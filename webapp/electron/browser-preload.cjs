// Preload for the in-app <webview> (partition persist:browser).
// Runs before page scripts. Defense-in-depth against Google/OAuth bot checks
// that look for Electron automation signals. The process-level
// disable-blink-features=AutomationControlled switch is the primary fix;
// this covers residual navigator leaks when that switch is unavailable.
"use strict";

try {
  const { webFrame } = require("electron");
  // Patch the page world (not the isolated preload world) so Google's
  // first-party scripts see a normal Chrome navigator.
  webFrame.executeJavaScript(
    `(() => {
      try {
        Object.defineProperty(navigator, "webdriver", {
          get: () => undefined,
          configurable: true,
        });
      } catch (_) {}
      try {
        if (!window.chrome) {
          window.chrome = { runtime: {} };
        } else if (!window.chrome.runtime) {
          window.chrome.runtime = {};
        }
      } catch (_) {}
    })();`,
    true,
  ).catch(() => {});
} catch (_) {
  // Preload must never throw into the webview.
}
