import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiTarget = process.env.TTS_MORE_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": apiTarget
    }
  }
});
