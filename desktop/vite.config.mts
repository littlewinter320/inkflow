import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  // Stable by default during real writing; opt in for isolated frontend work.
  server: {
    strictPort: true,
    port: Number(process.env.INKFLOW_DEV_PORT || 5173),
    hmr: process.env.INKFLOW_LIVE_RELOAD === "1",
    watch: process.env.INKFLOW_LIVE_RELOAD === "1" ? undefined : null,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
