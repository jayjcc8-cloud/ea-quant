/**
 * The preview's own Vite config: its own root, its own entry, no proxy.
 *
 * It deliberately sets no `server.proxy`, so nothing on this page can reach the
 * installed local service, and it is not referenced by the production build
 * entry or by `npm run build`.
 */
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const here = fileURLToPath(new URL('.', import.meta.url))
const parent = fileURLToPath(new URL('..', import.meta.url))

export default defineConfig({
  root: here,
  plugins: [react()],
  server: {
    port: 5174,
    // The preview root is inside the app; the parent is what it imports from.
    fs: { allow: [parent] },
  },
  build: {
    outDir: fileURLToPath(new URL('./dist', import.meta.url)),
    emptyOutDir: true,
  },
})
