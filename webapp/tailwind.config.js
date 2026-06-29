/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // lifted dark base with clear elevation steps (Hermes/Cursor-class
        // legibility: canvas is dark but not a void, each surface is visibly
        // lighter than the one below, secondary text stays readable).
        bg: "#1a1a1e", panel: "#222227", panel2: "#2c2c33",
        edge: "#3a3a45", edge2: "#4a4a57",
        txt: "#f4f4f8", muted: "#a8a8b5", faint: "#7c7c8a",
        // Restrained, desaturated accent (was a near-neon periwinkle #7c93ff,
        // which read as "vibe-coded"). A muted slate-blue carries interactivity
        // without shouting -- closer to the Cursor/Hermes register.
        accent: "#8590b0", accent2: "#22262f",
        // Softer status hues so tool rows read as professional, not candy. Errors
        // stay legibly warm; success/warn are desaturated.
        good: "#6fae8e", warn: "#c79a5e", risk: "#cf7d76",
      },
      fontFamily: {
        sans: ["-apple-system","BlinkMacSystemFont","Segoe UI","Roboto","sans-serif"],
        mono: ["ui-monospace","SFMono-Regular","Menlo","monospace"],
      },
    },
  },
  plugins: [],
}
