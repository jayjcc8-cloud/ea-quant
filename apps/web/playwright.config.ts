import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.EA_WEB_BASE_URL ?? 'http://127.0.0.1:8765',
    browserName: 'chromium',
    trace: 'on',
    screenshot: 'only-on-failure',
  },
})
