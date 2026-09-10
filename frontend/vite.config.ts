import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite 配置：React + 代理后端 API + 路径别名
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      // 把 /api 和 /ws 代理到后端，避免 CORS
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    chunkSizeWarningLimit: 800, // recharts 占 ~400KB，与 react 合并仍 < 800KB
    rollupOptions: {
      output: {
        // 把大依赖拆成独立 chunk，提升首屏加载速度
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
          charts: ["recharts"],
        },
      },
    },
  },
  resolve: {
    alias: {
      "@": "/src",
    },
  },
});
