/**
 * Contract tests for the Chat.jsx parallel-session tab wiring
 * (Kimi/GPT-style). Source-text style: pins the import, the tabs state,
 * the open-on-active-change effect, and the close handler.
 */
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { describe, it, expect } from 'vitest';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const src = readFileSync(resolve(__dirname, '../pages/Chat.jsx'), 'utf8');

describe('Chat.jsx parallel session tabs', () => {
  it('imports SessionTabs and the pure tab helpers', () => {
    expect(src).toMatch(/import SessionTabs from '@\/components\/chat\/SessionTabs';/);
    expect(src).toMatch(/import\s*\{\s*openTab,\s*closeTab\s*\}\s*from\s*'@\/lib\/sessionTabs';/);
  });

  it('holds a tabs state array', () => {
    expect(src).toMatch(/const \[tabs, setTabs\] = useState\(\[\]\);/);
  });

  it('opens a tab whenever the active session changes', () => {
    expect(src).toMatch(/openTab\(prev, activeId, sess\?\.title \|\| 'Chat'\)/);
    expect(src).toMatch(/}, \[activeId, sessions\]\);/);
  });

  it('closes a tab and falls back to a neighbor session', () => {
    expect(src).toMatch(/function handleCloseTab\(sessionId\) \{/);
    expect(src).toMatch(/closeTab\(tabs, sessionId\)/);
    expect(src).toMatch(/selectSession\(fallbackId\)/);
  });

  it('starts a fresh chat when the last tab is closed', () => {
    expect(src).toMatch(/contextNewChat\?\.\(\)/);
  });

  it('renders the tab strip above the chat header', () => {
    expect(src).toMatch(/<SessionTabs/);
    expect(src).toMatch(/onSelect=\{selectSession\}/);
    expect(src).toMatch(/onClose=\{handleCloseTab\}/);
  });
});
