/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Neutral slate surface used across the control-plane chrome.
        surface: {
          DEFAULT: "#0f172a",
          soft: "#1e293b",
          muted: "#334155",
        },
      },
    },
  },
  plugins: [],
};
