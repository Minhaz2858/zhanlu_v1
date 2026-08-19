const { test, expect } = require('@playwright/test');
const { login } = require('./helpers.cjs');

test.describe('Navigation smoke', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  for (const [url, heading] of [
    ['/automation', '自动化任务'],
    ['/my-space', '我的空间'],
    ['/market-dashboard/overview', '总览'],
  ]) {
    test(`route ${url} renders`, async ({ page }) => {
      await page.goto(url);
      await page.waitForLoadState('domcontentloaded');
      await expect(page).toHaveURL(new RegExp(url.replace(/\//g, '\\/')));
      await expect(page.locator('h1, h2, header').first()).toBeVisible({ timeout: 30_000 });
      await expect(page.getByText(heading).first()).toBeVisible({ timeout: 30_000 });
    });
  }

  test('sidebar navigation goes from chat to automation', async ({ page }) => {
    await page.getByRole('link', { name: '自动化任务' }).first().click();
    await expect(page).toHaveURL(/\/automation/);
    await expect(page.getByText('查看与管理在对话中创建的全部自动执行流程')).toBeVisible({ timeout: 30_000 });
  });
});
