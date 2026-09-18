// Pre-paint the chrome (or glass) before the module graph loads so a cold
// launch does not flash the UA-default page over vibrancy.
//
// This runs as a classic blocking script from index.html <head>. It used to be
// an inline <script>; it lives in its own file so the renderer can ship a
// Content-Security-Policy with `script-src 'self'` and no inline-script hash
// (see csp.ts / vite.config.ts). Keep it dependency-free and synchronous.
try {
  var THEMED = "#0f1113";
  var raw = localStorage.getItem("pmharness.translucency.v1");
  var state = raw ? JSON.parse(raw) : null;
  var glass = !!(state && state.mode !== "clear" && Number(state.intensity) > 0);
  document.documentElement.style.colorScheme = "dark";
  if (glass) {
    document.documentElement.setAttribute("data-marionette-glass", "");
    var keep = Math.max(0, 100 - Math.round(Number(state.intensity) || 0));
    document.documentElement.style.setProperty("--translucency-glass-keep", keep + "%");
    document.documentElement.style.backgroundColor = "transparent";
  } else {
    document.documentElement.style.backgroundColor = THEMED;
  }
} catch (e) {
  document.documentElement.style.backgroundColor = "#0f1113";
}
