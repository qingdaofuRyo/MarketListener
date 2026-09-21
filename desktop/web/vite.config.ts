import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  base: "/",
  build: {
    target: "chrome108",
    cssTarget: "chrome108",
    outDir: fileURLToPath(new URL("../src/market_monitor/web_dist", import.meta.url)),
    emptyOutDir: true,
  },
});
