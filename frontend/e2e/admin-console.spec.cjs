// spec: e2e/specs/admin-console.plan.md
// seed: real UI login via the password tab (no shared storageState)
//
// Generated with playwright-cli (@playwright/cli v0.1.19) against the live
// Zhanlu build on :8088 — see the emitted "Ran Playwright code" snippets.
// NOTE: two logins max per run (backend rate-limits /login to 5/min/IP), so
// route scenarios are grouped under one authenticated test instead of one
// spec file each (the plan's per-scenario files would burn the rate limit).
const { test, expect } = require('@playwright/test');

const ADMIN_EMAIL = process.env.E2E_EMAIL || 'admin@zhanlu.dev';
const ADMIN_PASSWORD = process.env.E2E_PASSWORD || 'admin123';
const BASE = process.env.E2E_BASE_URL || 'http://localhost:8088';
const LOGIN_RETRY_WAIT_MS = 61_000;

test.describe('Admin Console (Zhanlu :8088 build)', () => {
  // Fresh context — the shared .auth/user.json was captured on :8080 and its
  // origin state does not authenticate this build.
  test.use({ storageState: { cookies: [], origins: [] } });

  // Switch the tabbed login form (email-code default) to password mode, then
  // submit. Retries with backoff when the backend rate-limits /login.
  async function loginViaPasswordTab(page) {
    await page.addInitScript(() => {
      try { localStorage.setItem('zhanlu_lang', 'zh'); } catch {}
    });
    await page.goto(`${BASE}/login`);

    for (let attempt = 1; attempt <= 4; attempt++) {
      // Default mode is 邮箱验证码; reveal the password form via the inline
      // 使用密码登录 control inside the form.
      await page.locator('form').getByRole('button', { name: '密码' }).click();

      const emailBox = page.getByRole('textbox', { name: '邮箱' });
      const passwordBox = page.getByRole('textbox', { name: '密码' });
      await emailBox.fill(ADMIN_EMAIL);
      await passwordBox.fill(ADMIN_PASSWORD);

      const click = page.getByRole('button', { name: '登录' }).click();
      const landed = page.waitForURL((url) => url.pathname !== '/login', { timeout: 15_000 });
      const errorBox = page.locator('div[class*="bg-destructive"]').first();
      const outcome = await Promise.race([
        Promise.all([click, landed]).then(() => 'landed'),
        errorBox.waitFor({ state: 'visible', timeout: 15_000 }).then(() => 'error'),
      ]);

      if (outcome === 'landed') {
        await page.getByRole('heading', { name: 'User Management' }).waitFor({ timeout: 30_000 });
        return;
      }

      const msg = (await errorBox.textContent().catch(() => '')) || '';
      const rateLimited = /too many|429|rate/i.test(msg);
      console.log(`[login] attempt ${attempt} failed${rateLimited ? ' (rate-limited)' : ''}: ${msg.trim()}`);
      if (!rateLimited) throw new Error(`Login failed: ${msg.trim() || 'unknown error'}`);
      if (attempt < 4) await page.waitForTimeout(LOGIN_RETRY_WAIT_MS);
    }
    throw new Error('Login failed after 4 attempts (rate-limited).');
  }

  test('logs in via the password tab and shows User Management', async ({ page }) => {
    await loginViaPasswordTab(page);
    await expect(page).toHaveURL(/\/admin\/users/);
    await expect(page.getByRole('heading', { name: 'User Management' })).toBeVisible();
    // Seed accounts are listed in the users table.
    await expect(page.getByRole('table')).toContainText('admin@zhanlu.dev');
  });

  test('business + admin routes render their shells', async ({ page }) => {
    await loginViaPasswordTab(page);

    // 1. Market — agent catalog (zh UI: 市场 / 共 7 个智能体 / CAD代理)
    await page.goto(`${BASE}/market`);
    await expect(page.getByRole('heading', { name: '市场', level: 1 })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText('共 7 个智能体')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'CAD代理' })).toBeVisible();

    // 2. Toolkit — skills list (zh UI: 技能; skill ids stay ASCII)
    await page.goto(`${BASE}/toolkit`);
    await expect(page.getByRole('heading', { name: '技能', level: 1 })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('heading', { name: 'weekly-report-generation' })).toBeVisible();

    // 3. Agent Observability — invocation traces (heading stays English)
    await page.goto(`${BASE}/admin/observability`);
    await expect(page.getByRole('heading', { name: 'Agent Observability', level: 1 })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('main').getByRole('link').first()).toBeVisible();
  });
});
