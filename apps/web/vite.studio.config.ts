import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: fileURLToPath(new URL("./studio", import.meta.url)),
  plugins: [react()],
  cacheDir: fileURLToPath(
    new URL("../../.local/frontend-v3/vite-cache", import.meta.url),
  ),
  server: { host: "127.0.0.1", port: 4176, strictPort: true },
  build: {
    outDir: fileURLToPath(
      new URL("../../.local/frontend-v3/dist", import.meta.url),
    ),
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test.setup.ts"],
    globals: true,
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
