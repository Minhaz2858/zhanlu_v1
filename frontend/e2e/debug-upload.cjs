// Temporary diagnostic: perform the upload flow and capture console + network.
const { chromium } = require('@playwright/test');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ locale: 'zh-CN' });
  const logs = [];
  page.on('console', (m) => logs.push(`[console.${m.type()}] ${m.text()}`));
  page.on('pageerror', (e) => logs.push(`[pageerror] ${e.message}`));
  page.on('requestfailed', (r) => logs.push(`[reqfailed] ${r.url()} ${r.failure()?.errorText}`));
  page.on('response', (r) => {
    if (r.url().includes('upload') || r.url().includes('/files') || r.url().includes('user_files'))
      logs.push(`[resp ${r.status()}] ${r.url()}`);
  });

  await page.addInitScript(() => { try { localStorage.setItem('zhanlu_lang', 'zh'); } catch {} });
  await page.goto('http://localhost:8080/login');
  await page.fill('#email', 'admin@zhanlu.dev');
  await page.fill('#password', 'admin123');
  await page.getByRole('button', { name: /登录|log in/i }).click();
  await page.waitForURL((url) => url.pathname === '/', { timeout: 30000 });
  await page.getByTestId('chat-textarea').waitFor({ timeout: 30000 });

  await page.setInputFiles('input[data-testid="file-upload-input"]', {
    name: 'e2e-upload.txt', mimeType: 'text/plain', buffer: Buffer.from('hello e2e'),
  });
  await page.waitForTimeout(8000);

  const chipCount = await page.getByText('e2e-upload.txt').count();
  console.log('CHIP COUNT:', chipCount);
  const attachmentsInDom = await page.evaluate(() =>
    Array.from(document.querySelectorAll('button')).filter((b) => b.textContent.includes('e2e-upload')).length);
  console.log('ATTACH BUTTONS:', attachmentsInDom);
  const textarea = await page.getByTestId('chat-textarea').count();
  console.log('TEXTAREA COUNT:', textarea);
  const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 400).replace(/\n{2,}/g, '\n'));
  console.log('BODY TEXT:', bodyText);
  console.log('\n=== LOGS ===');
  console.log(logs.slice(0, 40).join('\n'));

  await browser.close();
})();
