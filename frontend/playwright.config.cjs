// E2E test configuration for the Zhanlu UI.
// The app is served by nginx at http://localhost:8080 (dist bind-mount).
// Override with E2E_BASE_URL to point at a dev server instead.
const path = require('path');
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  timeout: 180000,
  expect: { timeout: 30000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.E2E_RETRIES ? Number(process.env.E2E_RETRIES) : 0,
  reporter: [['list']],
  globalSetup: path.join(__dirname, 'e2e', 'global-setup.cjs'),
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:8080',
    headless: true,
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    storageState: path.join(__dirname, 'e2e', '.auth', 'user.json'),
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  outputDir: 'test-results',
});
