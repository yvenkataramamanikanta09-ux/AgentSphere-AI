import { defineConfig, devices } from '@playwright/test'

if (process.env.E2E_ISOLATED !== '1') {
  throw new Error('Use npm run test:e2e so browser tests run in a disposable database.')
}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 8000 },
  reporter: [['list'], ['html', { outputFolder: '.local/e2e/report', open: 'never' }]],
  outputDir: '.local/e2e/results',
  use: {
    baseURL: 'http://127.0.0.1:5174',
    trace: 'off', // Network traces and storage-state files can contain session secrets.
    video: 'off',
    screenshot: 'only-on-failure',
    channel: process.env.E2E_BROWSER_CHANNEL,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } }],
})
