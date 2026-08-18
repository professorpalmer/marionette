"use strict";

/**
 * Pure helpers for classic electron:dev renderer-load fallback.
 *
 * Classic `electron:dev` sets PMHARNESS_DEV_SERVER. Update & Relaunch re-execs
 * Electron after the old Vite owner exits, but the new process still has that
 * URL. A process-local latch abandons the configured origin after the first
 * non-aborted main-frame failure matching it; loadRenderer then uses dist and
 * never returns to the dead URL (including later restartBackend reloads).
 *
 * Does not flip isDev, delete env, start Vite, or change relaunch.
 */

/** Chromium ERR_ABORTED — navigation superseded; never treat as a dead origin. */
const ERR_ABORTED = -3;

/**
 * Scheme + host + port for comparison. Trailing slashes and paths collapse to
 * the same origin; another port or scheme does not. Invalid/empty → null.
 */
function normalizeRendererOrigin(url) {
  if (typeof url !== "string") return null;
  const trimmed = url.trim();
  if (!trimmed) return null;
  try {
    return new URL(trimmed).origin;
  } catch {
    return null;
  }
}

function configuredDevOriginMatches(configuredUrl, validatedUrl) {
  const configured = normalizeRendererOrigin(configuredUrl);
  const validated = normalizeRendererOrigin(validatedUrl);
  if (!configured || !validated) return false;
  return configured === validated;
}

/**
 * True when this did-fail-load should abandon the configured classic-dev URL.
 * Subframes, aborted navigations (-3), empty/invalid configured URLs, and
 * mismatched origins (other port/scheme/host) must not latch.
 */
function shouldAbandonConfiguredDevServer({
  isMainFrame,
  errorCode,
  validatedURL,
  configuredDevServerUrl,
} = {}) {
  if (isMainFrame !== true) return false;
  if (errorCode === ERR_ABORTED) return false;
  return configuredDevOriginMatches(configuredDevServerUrl, validatedURL);
}

/**
 * Existing 500 ms retry applies only to non-aborted main-frame failures while
 * the latch is unset. After abandonment, do not start a dist retry loop. A
 * matching configured-origin failure also must not retry the dead URL.
 */
function shouldRetryFailedRendererLoad({
  isMainFrame,
  errorCode,
  abandoned,
  validatedURL,
  configuredDevServerUrl,
} = {}) {
  if (isMainFrame !== true) return false;
  if (errorCode === ERR_ABORTED) return false;
  if (abandoned === true) return false;
  if (shouldAbandonConfiguredDevServer({
    isMainFrame,
    errorCode,
    validatedURL,
    configuredDevServerUrl,
  })) {
    return false;
  }
  return true;
}

/**
 * Classic-dev source selection. Before the latch, use the configured Vite URL;
 * after it (or when no URL is configured), use resolveDistIndex().
 */
function resolveClassicDevRendererSource({
  configuredDevServerUrl,
  abandoned,
} = {}) {
  if (abandoned) return "dist";
  if (typeof configuredDevServerUrl === "string" && configuredDevServerUrl.trim()) {
    return "dev";
  }
  return "dist";
}

/** Process-local latch: first matching abandon sticks for the life of this process. */
function createDevServerFallbackLatch() {
  let abandoned = false;
  return {
    isAbandoned() {
      return abandoned;
    },
    noteFailure(details) {
      if (abandoned) return false;
      if (!shouldAbandonConfiguredDevServer(details)) return false;
      abandoned = true;
      return true;
    },
  };
}

module.exports = {
  ERR_ABORTED,
  normalizeRendererOrigin,
  configuredDevOriginMatches,
  shouldAbandonConfiguredDevServer,
  shouldRetryFailedRendererLoad,
  resolveClassicDevRendererSource,
  createDevServerFallbackLatch,
};
