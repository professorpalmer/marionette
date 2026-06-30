/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // lifted dark base with clear elevation steps (Hermes/Cursor-class
        // legibility: canvas is dark but not a void, each surface is visibly
        // lighter than the one below, secondary text stays readable).
        // Surfaces carry a faint warm TEAL-charcoal undertone (Hermes-esque:
        // a deep teal/charcoal canvas, not flat neutral grey and not the old
        // cool blue-violet) so the canvas has DEPTH and a hair of warmth
        // instead of reading washed-out. Each step stays a clear elevation
        // above the one below.
        bg: "#14181a", panel: "#1c2226", panel2: "#262e32",
        edge: "#36403f", edge2: "#46524f",
        // Text keeps a hair of warmth so it doesn't read clinical on the base.
        txt: "#f4f5f3", muted: "#a9b0aa", faint: "#7c837d",
        // Accent: Hermes' signature WARM GOLD/AMBER -- the single biggest move
        // away from washed-out greyscale toward the teal-and-gold identity.
        // Reads as "interactive + considered + warm," not clinical.
        accent: "#d6a45c", accent2: "#2a2418",
        // Status hues: present and legible, re-saturated with a little life so
        // they read on the warm base. good leans teal-green to harmonize with
        // the canvas; warn echoes the gold accent; risk stays a warm coral.
        good: "#54bf95", warn: "#e0a94e", risk: "#dd7468",
      },
      fontFamily: {
        sans: ["-apple-system","BlinkMacSystemFont","Segoe UI","Roboto","sans-serif"],
        mono: ["ui-monospace","SFMono-Regular","Menlo","monospace"],
      },
    },
  },
  plugins: [],
}
