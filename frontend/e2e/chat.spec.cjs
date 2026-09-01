const { test, expect } = require('@playwright/test');
const { login } = require('./helpers.cjs');

test.describe('Chat E2E', () => {
  test('sends a message and receives an assistant reply', async ({ page }) => {
    test.setTimeout(240_000);
    await login(page);

    const textarea = page.getByTestId('chat-textarea');
    const question = '请用一句话介绍你自己';
    await textarea.fill(question);
    await page.getByTestId('btn-send').click();

    // The user message bubble shows the sent text.
    await expect(page.getByTestId('msg-user').last()).toContainText(question, { timeout: 20_000 });

    // An assistant bubble must appear and eventually hold non-empty content.
    const reply = page.getByTestId('msg-assistant').last();
    await expect(reply).toBeVisible({ timeout: 150_000 });
    await expect(reply).not.toHaveText(/^\s*$/, { timeout: 150_000 });
  });
});
