/** Tailwind CSS 配置。 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 涨绿跌红（中国市场惯例，区别于欧美）
        up: { DEFAULT: "#ef4444", soft: "#7f1d1d" },  // 红涨
        down: { DEFAULT: "#10b981", soft: "#064e3e" }, // 绿跌
        flat: "#94a3b8",
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', '"SF Mono"', "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
