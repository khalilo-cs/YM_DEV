import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the platform at /app/ (see api/app.py). No external CDNs: works fully offline.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
