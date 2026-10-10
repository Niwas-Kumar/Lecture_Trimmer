import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built files ship inside the Python package and are served at /studio.
export default defineConfig({
  base: "/studio/",
  plugins: [react()],
  build: {
    outDir: "../lecture_trimmer/web/dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    // `npm run dev` talks to a running app (python app.py --port 7861).
    proxy: { "/api": "http://127.0.0.1:7861", "/auth": "http://127.0.0.1:7861" },
  },
});
