// Shared helpers for E2E specs.
// Credentials can be overridden via E2E_EMAIL / E2E_PASSWORD.
const ADMIN_EMAIL = process.env.E2E_EMAIL || 'admin@zhanlu.dev';
const ADMIN_PASSWORD = process.env.E2E_PASSWORD || 'admin123';

// The backend rate-limits the login endpoint to 5/min per IP (in-memory).
// Rapid suite runs can exhaust the window, so retry with a backoff.
const LOGIN_RETRY_WAIT_MS = 61_000;

// Navigate to the app and ensure the chat home is ready.
// Most specs inherit an authenticated storageState (set up once in
// global-setup.cjs), so this just lands on the chat page. When no session
// exists yet (e.g. auth.spec opts out of storageState), it performs a real
// login through the UI, retrying if the backend rate-limits us.
async function login(page, { email = ADMIN_EMAIL, password = ADMIN_PASSWORD } = {}) {
  // The app syncs the UI language from the user's server setting, which may be
  // 'en'. Force the stored language to Chinese so the specs can assert the zh
  // labels deterministically.
  await page.addInitScript(() => {
    try { localStorage.setItem('zhanlu_lang', 'zh'); } catch {}
  });

  await page.goto('/');
  // The SPA may take a moment to decide between rendering the chat home or
  // redirecting to /login. Wait for whichever appears, then branch.
  const loginForm = page.locator('#email');
  const composer = page.getByTestId('chat-textarea');
  await Promise.race([
    loginForm.waitFor({ state: 'visible', timeout: 30_000 }),
    composer.waitFor({ state: 'visible', timeout: 30_000 }),
  ]);
  if (await composer.count()) return;

  for (let attempt = 1; attempt <= 4; attempt++) {
    await page.fill('#email', email);
    await page.fill('#password', password);
    const click = page.getByRole('button', { name: /登录|log in/i }).click();
    const landed = page.waitForURL((url) => url.pathname === '/', { timeout: 15_000 });
    const errorBox = page.locator('div[class*="bg-destructive"]').first();
    const outcome = await Promise.race([
      Promise.all([click, landed]).then(() => 'landed'),
      errorBox.waitFor({ state: 'visible', timeout: 15_000 }).then(() => 'error'),
    ]);

    if (outcome === 'landed') {
      await page.getByTestId('chat-textarea').waitFor({ timeout: 30_000 });
      return;
    }

    const msg = (await errorBox.textContent().catch(() => '')) || '';
    const rateLimited = /too many|429|rate/i.test(msg);
    console.log(`[login] attempt ${attempt} failed${rateLimited ? ' (rate-limited)' : ''}: ${msg.trim()}`);
    if (!rateLimited) {
      throw new Error(`Login failed: ${msg.trim() || 'unknown error'}`);
    }
    if (attempt < 4) await page.waitForTimeout(LOGIN_RETRY_WAIT_MS);
  }
  throw new Error('Login failed after 4 attempts (rate-limited).');
}

module.exports = { login, ADMIN_EMAIL, ADMIN_PASSWORD };
