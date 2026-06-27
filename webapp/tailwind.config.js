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
        accent: "#7c93ff", accent2: "#1e2436",
        good: "#3ecf8e", warn: "#e0a44a", risk: "#e0625c",
      },
      fontFamily: {
        sans: ["-apple-system","BlinkMacSystemFont","Segoe UI","Roboto","sans-serif"],
        mono: ["ui-monospace","SFMono-Regular","Menlo","monospace"],
      },
    },
  },
  plugins: [],
}
