/**
 * Contract tests for the SessionList conversation share (Kimi/GPT-style).
 * Source-text style: pins the Share menu item, the chatShare helper call,
 * the clipboard copy, and the toast feedback.
 */
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { describe, it, expect } from 'vitest';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const src = readFileSync(resolve(__dirname, './SessionList.jsx'), 'utf8');

describe('SessionList conversation share', () => {
  it('imports chatShare from agentEnhanced', () => {
    expect(src).toMatch(/import\s*\{\s*chatSearch,\s*chatShare\s*\}\s*from\s*'@\/api\/agentEnhanced'/);
  });

  it('imports the Link2 icon and sonner toast', () => {
    expect(src).toMatch(/import\s*\{[^}]*\bLink2\b[^}]*\}\s*from\s*'lucide-react'/);
    expect(src).toMatch(/import\s*\{\s*toast\s*\}\s*from\s*'sonner'/);
  });

  it('renders a Share item in the per-session menu', () => {
    expect(src).toMatch(/handleShareSession\(s\)/);
    expect(src).toMatch(/t\.sessionList\.share/);
    expect(src).toMatch(/<Link2 className="h-3 w-3" \/> \{t\.sessionList\.share\}/);
  });

  it('creates the share and copies the absolute URL to the clipboard', () => {
    expect(src).toMatch(/const \{ share_url \} = await chatShare\(s\.id\);/);
    expect(src).toMatch(/window\.location\.origin/);
    expect(src).toMatch(/navigator\.clipboard\.writeText\(url\)/);
  });

  it('shows a success toast with the URL and an error toast on failure', () => {
    expect(src).toMatch(/toast\.success\(t\.sessionList\.shareCopied,\s*\{\s*description:\s*url\s*\}\)/);
    expect(src).toMatch(/toast\.error\(t\.sessionList\.shareFailed\)/);
  });
});
