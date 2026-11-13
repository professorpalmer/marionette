/** The one font list. tailwind.config.js reads the arrays for `font-sans` and
 *  `font-mono` (and CSS `theme('fontFamily.*')`). Canvas measure, xterm and
 *  CodeMirror read the CSS strings.
 *
 *  Windows has no SF font and no Menlo. Chromium also ignores `ui-monospace`.
 *  So Windows uses Segoe UI for sans, and Cascadia Mono (Windows 11) or
 *  Consolas (every Windows) for mono. */
export const SANS_FONTS = ["-apple-system", "BlinkMacSystemFont", "Segoe UI", "Roboto", "sans-serif"];
export const MONO_FONTS = ["ui-monospace", "SFMono-Regular", "Menlo", "Cascadia Mono", "Consolas", "Liberation Mono", "monospace"];

function cssFontList(fonts: readonly string[]): string {
  return fonts.map((f) => (/\s/.test(f) ? `"${f}"` : f)).join(", ");
}

export const SANS_STACK = cssFontList(SANS_FONTS);
export const MONO_STACK = cssFontList(MONO_FONTS);
