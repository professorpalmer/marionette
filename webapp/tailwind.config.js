/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0d0d0f", panel: "#141417", panel2: "#1a1a1f",
        edge: "#26262d", txt: "#e6e6ea", muted: "#8a8a94",
        accent: "#6aa6ff", accent2: "#2b3a55",
        good: "#4caf7d", warn: "#d9a441", risk: "#d9645f",
      },
      fontFamily: {
        sans: ["-apple-system","BlinkMacSystemFont","Segoe UI","Roboto","sans-serif"],
        mono: ["ui-monospace","SFMono-Regular","Menlo","monospace"],
      },
    },
  },
  plugins: [],
}
