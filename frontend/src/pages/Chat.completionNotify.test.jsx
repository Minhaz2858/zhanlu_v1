/**
 * Contract tests for the Chat.jsx completion-notification wiring
 * (Kimi/GPT-style). Source-text style: pins the import, the central
 * onRunComplete helper, its call sites, and the permission request.
 */
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { describe, it, expect } from 'vitest';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const src = readFileSync(resolve(__dirname, '../pages/Chat.jsx'), 'utf8');

describe('Chat.jsx completion notifications', () => {
  it('imports the pure decision helpers from completionNotify', () => {
    expect(src).toMatch(/import\s*\{\s*shouldNotify,\s*notificationBody\s*\}\s*from\s*'@\/lib\/completionNotify'/);
  });

  it('defines a central onRunComplete with a per-run dedupe set', () => {
    expect(src).toMatch(/const notifiedRunIds = useRef\(new Set\(\)\);/);
    expect(src).toMatch(/function onRunComplete\(runId, finalText\) \{/);
    expect(src).toMatch(/notifiedRunIds\.current\.has\(runId\)/);
    expect(src).toMatch(/new Notification\('Zhanlu',\s*\{\s*body:\s*notificationBody\(finalText\)\s*\}\)/);
  });

  it('only fires when the tab is hidden and permission is granted', () => {
    expect(src).toMatch(/shouldNotify\(\{/);
    expect(src).toMatch(/document\.hidden/);
    expect(src).toMatch(/Notification\.permission/);
  });

  it('fires on the success path with the final assistant reply', () => {
    expect(src).toMatch(/onRunComplete\(aiMsg\.id, reply\);/);
  });

  it('fires on the error path with the friendly error message', () => {
    expect(src).toMatch(/onRunComplete\(errMsg\.id, errMsg\.content \|\| ''\);/);
  });

  it('requests permission once on the first send (user gesture)', () => {
    expect(src).toMatch(/Notification\.permission === 'default'/);
    expect(src).toMatch(/Notification\.requestPermission\(\);/);
  });
});
