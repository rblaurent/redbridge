import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5373,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:47337",
      "/hook": "http://127.0.0.1:47337",
      "/ws": { target: "ws://127.0.0.1:47337", ws: true },
    },
  },
  build: { outDir: "dist" },
});
