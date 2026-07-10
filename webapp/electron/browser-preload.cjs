// Preload for the in-app <webview> / browser popouts (partition persist:browser).
// Runs in the guest before page scripts. Defense-in-depth against Google/OAuth
// bot checks that look for Electron automation signals. The process-level
// disable-blink-features=AutomationControlled switch is the primary fix; this
// covers residual navigator / chrome / UA-CH leaks BEFORE Google's first paint
// decision (do not rely only on late main-process dom-ready injection).
"use strict";

try {
  const { webFrame } = require("electron");

  // Page-world stealth. Kept as one IIFE so it lands before first-party scripts
  // observe navigator. Must stay self-contained (no closure over Node).
  const STEALTH = `(() => {
    try {
      if (window.__pmAutomationHidden) return;
      window.__pmAutomationHidden = true;

      try {
        Object.defineProperty(navigator, "webdriver", {
          get: () => undefined,
          configurable: true,
        });
      } catch (_) {}

      try {
        if (!window.chrome) {
          window.chrome = {
            runtime: {},
            loadTimes: function () { return {}; },
            csi: function () { return {}; },
            app: {},
          };
        } else {
          if (!window.chrome.runtime) window.chrome.runtime = {};
          if (typeof window.chrome.loadTimes !== "function") {
            window.chrome.loadTimes = function () { return {}; };
          }
          if (typeof window.chrome.csi !== "function") {
            window.chrome.csi = function () { return {}; };
          }
          if (!window.chrome.app) window.chrome.app = {};
        }
      } catch (_) {}

      try {
        const plugins = navigator.plugins;
        if (!plugins || plugins.length === 0) {
          const fake = [
            {
              name: "Chrome PDF Plugin",
              filename: "internal-pdf-viewer",
              description: "Portable Document Format",
            },
            {
              name: "Chrome PDF Viewer",
              filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai",
              description: "",
            },
            {
              name: "Native Client",
              filename: "internal-nacl-plugin",
              description: "",
            },
          ];
          fake.item = (i) => fake[i] || null;
          fake.namedItem = (n) => fake.find((p) => p.name === n) || null;
          fake.refresh = () => {};
          Object.defineProperty(navigator, "plugins", {
            get: () => fake,
            configurable: true,
          });
        }
      } catch (_) {}

      try {
        if (!navigator.languages || navigator.languages.length === 0) {
          Object.defineProperty(navigator, "languages", {
            get: () => Object.freeze(["en-US", "en"]),
            configurable: true,
          });
        }
      } catch (_) {}

      try {
        const mimeTypes = navigator.mimeTypes;
        if (!mimeTypes || mimeTypes.length === 0) {
          const fake = [
            {
              type: "application/pdf",
              suffixes: "pdf",
              description: "Portable Document Format",
            },
            {
              type: "application/x-google-chrome-pdf",
              suffixes: "pdf",
              description: "Portable Document Format",
            },
          ];
          fake.item = (i) => fake[i] || null;
          fake.namedItem = (n) => fake.find((m) => m.type === n) || null;
          Object.defineProperty(navigator, "mimeTypes", {
            get: () => fake,
            configurable: true,
          });
        }
      } catch (_) {}

      // Align navigator.userAgentData with a normal Chrome surface when missing
      // or empty (Electron historically left brands empty -> Sec-CH mismatch).
      try {
        const ua = String(navigator.userAgent || "");
        const m = ua.match(/Chrome\\/([\\d.]+)/);
        const full = (m && m[1]) || "130.0.0.0";
        const major = full.split(".")[0] || "130";
        let platform = "Windows";
        if (/Macintosh|Mac OS X/i.test(ua)) platform = "macOS";
        else if (/Linux|X11/i.test(ua)) platform = "Linux";
        const brands = [
          { brand: "Not_A Brand", version: "8" },
          { brand: "Chromium", version: major },
          { brand: "Google Chrome", version: major },
        ];
        const fullVersionList = [
          { brand: "Not_A Brand", version: "10.0.0.0" },
          { brand: "Chromium", version: full },
          { brand: "Google Chrome", version: full },
        ];
        const highEntropy = {
          architecture: "x86",
          bitness: "64",
          brands,
          fullVersion: full,
          fullVersionList,
          mobile: false,
          model: "",
          platform,
          platformVersion: platform === "Windows" ? "15.0.0" : "13.0.0",
          uaFullVersion: full,
        };
        const uaData = {
          brands,
          mobile: false,
          platform,
          getHighEntropyValues: (hints) => {
            const out = { brands, mobile: false, platform };
            const list = Array.isArray(hints) ? hints : [];
            for (const h of list) {
              if (Object.prototype.hasOwnProperty.call(highEntropy, h)) {
                out[h] = highEntropy[h];
              }
            }
            return Promise.resolve(out);
          },
          toJSON: () => ({ brands, mobile: false, platform }),
        };
        const existing = navigator.userAgentData;
        const brandsEmpty =
          !existing ||
          !Array.isArray(existing.brands) ||
          existing.brands.length === 0;
        if (brandsEmpty) {
          Object.defineProperty(Navigator.prototype, "userAgentData", {
            get: () => uaData,
            configurable: true,
          });
        }
      } catch (_) {}

      try {
        if (navigator.permissions && navigator.permissions.query) {
          const orig = navigator.permissions.query.bind(navigator.permissions);
          navigator.permissions.query = (params) => {
            try {
              if (params && params.name === "notifications") {
                return Promise.resolve({
                  state: Notification.permission || "default",
                  onchange: null,
                });
              }
            } catch (_) {}
            return orig(params);
          };
        }
      } catch (_) {}
    } catch (_) {}
  })();`;

  // Fire immediately (preload time) so the page world is patched before Google
  // first-party scripts decide. userGesture=true avoids some CSP balking.
  webFrame.executeJavaScript(STEALTH, true).catch(() => {});
} catch (_) {
  // Preload must never throw into the webview.
}
