/**
 * Contract tests for the SessionTabs component (Kimi/GPT-style parallel
 * conversation tabs). Source-text style: pins the tab strip testids,
 * active highlight, close button, and empty-state return.
 */
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { describe, it, expect } from 'vitest';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const src = readFileSync(resolve(__dirname, './SessionTabs.jsx'), 'utf8');

describe('SessionTabs component', () => {
  it('renders nothing when there are no tabs', () => {
    expect(src).toMatch(/if \(!tabs \|\| tabs\.length === 0\) return null;/);
  });

  it('renders the tab strip with a stable testid', () => {
    expect(src).toMatch(/data-testid="session-tabs"/);
  });

  it('renders one tab per session with per-session testids', () => {
    expect(src).toMatch(/data-testid=\{`session-tab-\$\{t\.sessionId\}`\}/);
    expect(src).toMatch(/tabs\.map\(\(t\) =>/);
  });

  it('marks the active tab with a data-active attribute', () => {
    expect(src).toMatch(/data-active=\{activeId === t\.sessionId \? 'true' : 'false'\}/);
  });

  it('selects the session on tab click', () => {
    expect(src).toMatch(/onClick=\{\(\) => onSelect\?\.\(t\.sessionId\)\}/);
  });

  it('closes via the per-tab close button without selecting', () => {
    expect(src).toMatch(/data-testid=\{`session-tab-close-\$\{t\.sessionId\}`\}/);
    expect(src).toMatch(/e\.stopPropagation\(\);/);
    expect(src).toMatch(/onClose\?\.\(t\.sessionId\)/);
  });
});
