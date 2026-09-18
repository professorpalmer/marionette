/**
 * The renderer Content-Security-Policy.
 *
 * Single source of truth, imported by vite.config.ts (which injects it into the
 * production index.html as a meta tag -- the app is loaded over file:// in the
 * packaged Electron shell, where a webRequest response header never fires) and
 * by src/__tests__/csp.test.ts (which pins the directives so a careless edit
 * cannot silently drop the guard).
 *
 * Directive rationale -- every relaxation here is load-bearing for a shipped
 * feature, so do not tighten one without checking the feature:
 *   script-src 'self'      no inline scripts (prepaint.js is an external file
 *                          precisely so this can stay strict) and no eval.
 *   style-src  'unsafe-inline'
 *                          Tailwind/React set style attributes at runtime;
 *                          blocking that buys nothing here.
 *   connect-src loopback   the renderer talks to the local harness backend.
 *                          ws: covers the dev-server HMR socket.
 *   frame-src  *           the Browser pane renders arbitrary user-navigated
 *                          sites in a <webview>. Closing this would break the
 *                          product's browser; those guests are isolated by
 *                          hardenWebPreferences + their own sandboxed preload.
 *   form-action 'self'     the app has real <form> submits (ScheduleEditor,
 *                          RegistryWizard, MemoryPane, OnboardingOverlay,
 *                          CheckpointsPane) -- 'none' would break them.
 *   object-src 'none'      no plugins/embeds; pure win.
 *   base-uri   'none'      nothing in the app uses <base>; blocks base-tag
 *                          hijacking of relative asset URLs.
 */

/** Extra origins the shipped renderer legitimately loads from.
 *
 * Deliberately NOT listed: `http://[::1]:*` / `ws://[::1]:*`. Chromium rejects a
 * bracketed-IPv6 host combined with a wildcard port as an invalid CSP source
 * ("contains an invalid source ... It will be ignored", verified in a real
 * file:// load), so listing it is noise at best. The renderer only ever targets
 * 127.0.0.1 (and localhost via the dev proxy); the harness binds IPv4 loopback.
 */
export const LOOPBACK_ORIGINS = [
  "http://127.0.0.1:*",
  "http://localhost:*",
];

const WS_LOOPBACK_ORIGINS = [
  "ws://127.0.0.1:*",
  "ws://localhost:*",
];

/**
 * file: is listed explicitly on the resource directives because a document
 * loaded from file:// has an opaque-ish origin that Chromium does not always
 * match against 'self'; without it the packaged app renders blank. This is why
 * `npm run build` + a real Electron load is part of verifying this file.
 */
export const CSP_DIRECTIVES: Record<string, string[]> = {
  "default-src": ["'self'", "file:"],
  "script-src": ["'self'", "file:"],
  "style-src": ["'self'", "'unsafe-inline'", "file:"],
  "img-src": ["'self'", "data:", "blob:", "file:"],
  "font-src": ["'self'", "data:", "file:"],
  "media-src": ["'self'", "data:", "blob:", "file:"],
  "connect-src": ["'self'", "file:", ...LOOPBACK_ORIGINS, ...WS_LOOPBACK_ORIGINS],
  "worker-src": ["'self'", "blob:"],
  "frame-src": ["*"],
  "object-src": ["'none'"],
  "base-uri": ["'none'"],
  "form-action": ["'self'"],
};

/** Serialize to a `Content-Security-Policy` header/meta value. */
export function cspPolicy(): string {
  return Object.entries(CSP_DIRECTIVES)
    .map(([directive, sources]) => `${directive} ${sources.join(" ")}`)
    .join("; ");
}

/** The <meta> tag injected into the production index.html. */
export function cspMetaTag(): string {
  return `<meta http-equiv="Content-Security-Policy" content="${cspPolicy()}" />`;
}
