// Unpacked MV3 worker. Posts the same tab_snapshot shape the native host uses.
// Unpacked only; no store listing or third-party telemetry.
const KIND = "tab_snapshot";

function snapshotFromTab(tab, source) {
  return {
    kind: KIND,
    url: tab && tab.url ? String(tab.url) : "",
    title: tab && tab.title ? String(tab.title) : "",
    tab_id: tab && tab.id != null ? tab.id : null,
    source: source || "extension",
  };
}

function postToHarness(message) {
  const base = (self.HARNESS_RELAY_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
  const token = self.HARNESS_TOKEN || "";
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-Harness-Token"] = token;
  return fetch(base + "/api/browser/relay", {
    method: "POST",
    headers,
    body: JSON.stringify(message),
  }).catch(function () { return null; });
}

function sendNative(message) {
  if (!chrome.runtime || !chrome.runtime.sendNativeMessage) return;
  try {
    chrome.runtime.sendNativeMessage("ai.marionette.browser_relay", message, function () {
      void chrome.runtime.lastError;
    });
  } catch (_err) {
    /* native host is optional */
  }
}

function relayTab(tab) {
  const message = snapshotFromTab(tab, "extension");
  if (!message.url) return;
  sendNative(message);
  postToHarness(message);
}

if (chrome.tabs && chrome.tabs.onUpdated) {
  chrome.tabs.onUpdated.addListener(function (_id, change, tab) {
    if (change.status === "complete" || change.title || change.url) {
      relayTab(tab);
    }
  });
}
