import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{js,ts,jsx,tsx,mdx}", "./components/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--dualith-bg)",
        surface: "var(--dualith-surface)",
        line: "var(--dualith-line)",
        "line-hard": "var(--dualith-line-hard)",
        text: "var(--dualith-text)",
        "text-strong": "var(--dualith-text-strong)",
        "text-soft": "var(--dualith-text-soft)",
        "text-muted": "var(--dualith-text-muted)",
        "text-faint": "var(--dualith-text-faint)",
        muted: "var(--dualith-muted)",
        accent: "var(--dualith-cyan)",
        ok: "var(--dualith-emerald)",
        warn: "var(--dualith-amber)",
        danger: "var(--dualith-red)"
      },
      keyframes: {
        "pulse-glow": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.45" }
        }
      },
      animation: {
        "pulse-glow": "pulse-glow 1.4s ease-in-out infinite"
      }
    }
  },
  plugins: []
};

export default config;
