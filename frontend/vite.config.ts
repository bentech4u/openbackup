import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API to a locally running `openbackup serve`.
const api = process.env.OPENBACKUP_API ?? "https://127.0.0.1:8443";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../openbackup/web",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: { "/api": { target: api, secure: false, changeOrigin: true } },
  },
});
