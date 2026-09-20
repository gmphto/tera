import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The service's Origin allowlist names http://localhost:1420, so the dev server
// is pinned there: strictPort makes Vite fail rather than move to another port
// and leave every request refused by #27's policy.
export default defineConfig({
  plugins: [react()],
  base: "./",
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/src-tauri/**"] },
  },
  build: { outDir: "dist" },
  test: { environment: "node", include: ["src/**/*.test.ts", "src/**/*.test.tsx"] },
});
