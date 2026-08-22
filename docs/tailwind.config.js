export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#05060A",
        surface: "#0B0E16",
        raised: "#111726",
        line: "#1B2233",
        "line-bright": "#2B3550",
        ink: "#E6EDF7",
        muted: "#8B97AD",
        faint: "#7C8798",

        cyan: "#22D3EE",
        magenta: "#FF2FD0",
        violet: "#8A2BE2",
        lime: "#D7FF64",

        critical: "#FF2FD0",
        major: "#FF8A3D",
        minor: "#F5D90A",
        info: "#22D3EE",
      },
      fontFamily: {
        sans: ["Roboto", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["Roboto Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      maxWidth: {
        prose: "46rem",
        shell: "88rem",
      },
    },
  },
  plugins: [],
};
