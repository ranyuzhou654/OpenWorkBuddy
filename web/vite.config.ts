import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built UI ships inside the Python package; `nwb serve` serves it.
// In development, `npm run dev` proxies /api to a running `nwb serve`.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/nano_workbuddy/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
