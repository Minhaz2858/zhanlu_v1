/**
 * Contract tests for the SessionList conversation export (Kimi/GPT-style).
 * Source-text style: pins the Download menu item, the handler wiring,
 * and the downloadConversationMarkdown usage.
 */
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { describe, it, expect } from 'vitest';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const src = readFileSync(resolve(__dirname, './SessionList.jsx'), 'utf8');

describe('SessionList conversation export', () => {
  it('imports downloadConversationMarkdown from the export lib', () => {
    expect(src).toMatch(/import\s*\{\s*downloadConversationMarkdown\s*\}\s*from\s*'@\/lib\/exportConversation'/);
  });

  it('imports the Download icon from lucide-react', () => {
    expect(src).toMatch(/import\s*\{[^}]*\bDownload\b[^}]*\}\s*from\s*'lucide-react'/);
  });

  it('renders an Export item in the per-session menu', () => {
    expect(src).toMatch(/handleExportSession\(s\)/);
    expect(src).toMatch(/t\.sessionList\.export/);
    expect(src).toMatch(/<Download className="h-3 w-3" \/> \{t\.sessionList\.export\}/);
  });

  it('fetches the session messages before downloading', () => {
    expect(src).toMatch(/base44\.entities\.ChatMessage\.filter\(\{\s*session_id:\s*s\.id\s*\},?\s*'created_date',?\s*200\)/);
  });

  it('is best-effort — a fetch failure never blocks the menu', () => {
    expect(src).toMatch(/catch \{\s*\/\* export is best-effort — never block the menu \*\/\s*\}/);
    expect(src).toMatch(/setMenuFor\(null\);/);
  });
});
