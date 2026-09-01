const { test, expect } = require('@playwright/test');
const { login } = require('./helpers.cjs');
const fs = require('fs');
const os = require('os');
const path = require('path');

test.describe('File upload', () => {
  test('shows an attachment chip after uploading a file', async ({ page }) => {
    await login(page);
    await expect(page.getByTestId('chat-textarea')).toBeVisible({ timeout: 30_000 });

    const fileName = 'e2e-upload.txt';
    const tmp = path.join(os.tmpdir(), fileName);
    fs.writeFileSync(tmp, 'hello from e2e', 'utf8');

    await page.getByTestId('file-upload-input').setInputFiles(tmp);

    // The attachment chip in the composer shows the file name.
    await expect(page.getByText(fileName)).toBeVisible({ timeout: 30_000 });
  });
});
