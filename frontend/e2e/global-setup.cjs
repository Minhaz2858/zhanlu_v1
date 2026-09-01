// Global setup: log in ONCE through the real UI and save the session
// (cookies + localStorage) so every spec starts authenticated. This avoids
// hammering the backend login endpoint, which is rate-limited to 5/min/IP.
const { chromium } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const STATE_DIR = path.join(__dirname, '.auth');
const STATE_FILE = path.join(STATE_DIR, 'user.json');

const ADMIN_EMAIL = process.env.E2E_EMAIL || 'admin@zhanlu.dev';
const ADMIN_PASSWORD = process.env.E2E_PASSWORD || 'admin123';

module.exports = async () => {
  fs.mkdirSync(STATE_DIR, { recursive: true });

  const browser = await chromium.launch();
  const page = await browser.newPage();
  // Force the zh UI so specs can assert Chinese labels deterministically.
  await page.addInitScript(() => {
    try { localStorage.setItem('zhanlu_lang', 'zh'); } catch {}
  });

  await page.goto('http://localhost:8080/login');
  await page.fill('#email', ADMIN_EMAIL);
  await page.fill('#password', ADMIN_PASSWORD);
  await Promise.all([
    page.waitForURL((url) => url.pathname === '/', { timeout: 30_000 }),
    page.getByRole('button', { name: /登录|log in/i }).click(),
  ]);
  await page.getByTestId('chat-textarea').waitFor({ timeout: 30_000 });

  await page.context().storageState({ path: STATE_FILE });
  await browser.close();
};
