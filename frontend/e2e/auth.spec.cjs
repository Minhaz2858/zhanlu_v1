const { test, expect } = require('@playwright/test');
const { login } = require('./helpers.cjs');

// These tests exercise the real login flow, so opt out of the shared
// authenticated storageState (a fresh browser context with no session).
test.describe('Authentication', () => {
  test.use({ storageState: { cookies: [], origins: [] } });
  test('rejects invalid credentials with an inline error', async ({ page }) => {
    await page.goto('/login');
    await page.fill('#email', 'nobody@example.com');
    await page.fill('#password', 'definitely-wrong');
    await page.getByRole('button', { name: /登录|log in/i }).click();

    await expect(page.locator('div[class*="bg-destructive"]')).toBeVisible({ timeout: 20_000 });
    await expect(page).toHaveURL(/\/login/);
  });

  test('logs in with valid credentials and lands on the chat home', async ({ page }) => {
    await login(page);
    await expect(page).toHaveURL('/');
    await expect(page.getByTestId('chat-textarea')).toBeVisible({ timeout: 30_000 });
  });
});
