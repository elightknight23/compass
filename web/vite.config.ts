import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs on uvicorn :8000 and Vite proxies /api to it,
// so the session cookie is same-origin from the browser's point of view.
// In production FastAPI serves the built files from web/dist itself.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
