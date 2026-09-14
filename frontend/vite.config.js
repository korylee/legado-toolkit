import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 开发时把 /api 代理到后端，避免 CORS；
// 生产时前端由后端托管（同源），也不需要 CORS。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": {
        target: process.env.VITE_BACKEND || "http://127.0.0.1:8787",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 1200 },
});
