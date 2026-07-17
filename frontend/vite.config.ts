import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiTarget = process.env.TTS_MORE_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "react-vendor": ["react", "react-dom"],
          "i18n-vendor": ["i18next", "react-i18next", "i18next-browser-languagedetector"],
          "audio-vendor": ["wavesurfer.js"],
          "icons-vendor": ["lucide-react"]
        }
      }
    }
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": apiTarget
    }
  }
});
