/**
 * MessageBubble.jsx source chips — Kimi/GPT-style live source links:
 * a source carrying `url` (web_search results, generated file URLs) must
 * render as a clickable anchor (new tab) instead of a plain span, so the
 * user can open the source the answer is grounded in.
 *
 * Source-text contract tests — same pattern as MessageBubble.attachments.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './MessageBubble.jsx'), 'utf8');

describe('MessageBubble.jsx source chips', () => {
  it('maps over message.sources with a url-aware body', () => {
    expect(SOURCE).toMatch(/message\.sources\.slice\(0,\s*6\)\.map\(\(src,\s*i\)\s*=>\s*\{/);
  });

  it('computes srcUrl from src.url (http/https only)', () => {
    expect(SOURCE).toMatch(/srcUrl\s*=\s*typeof src\.url\s*===\s*'string'\s*&&\s*\/\^https\?:\\\/\\\/\/\.test\(src\.url\)\s*\?\s*src\.url\s*:\s*null/);
  });

  it('renders a clickable link chip when srcUrl is set', () => {
    const linkBlock = SOURCE.match(
      /if\s*\(srcUrl\)\s*\{[\s\S]{0,900}?data-testid="source-chip-link"/,
    );
    expect(linkBlock, 'source-chip-link anchor not found').not.toBeNull();
    expect(linkBlock[0]).toMatch(/href=\{srcUrl\}/);
    expect(linkBlock[0]).toMatch(/target="_blank"/);
    expect(linkBlock[0]).toMatch(/rel="noopener noreferrer"/);
  });

  it('uses the ExternalLink icon for url sources and Table2 for plain sources', () => {
    expect(SOURCE).toMatch(/srcUrl\s*\n?\s*\?\s*<ExternalLink/);
    expect(SOURCE).toMatch(/: <Table2 className="h-3 w-3 shrink-0" \/>/);
  });

  it('keeps the row-count label for data sources', () => {
    expect(SOURCE).toMatch(/\{typeof src\.rows === 'number' &&/);
    expect(SOURCE).toMatch(/\{src\.rows\} rows/);
  });

  it('falls back to a plain span chip when no url is present', () => {
    const spanBlock = SOURCE.match(
      /return \(\s*<span\s+key=\{`\$\{src\.source_id \|\| src\.source_name \|\| ''\}-\$\{i\}`\}[\s\S]{0,300}?\{chipInner\}/,
    );
    expect(spanBlock, 'plain span chip not found').not.toBeNull();
  });
});
