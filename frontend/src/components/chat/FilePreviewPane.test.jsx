/**
 * FilePreviewPane.jsx — right-side preview pane for user-attached files
 * (Kimi/ChatGPT-style). Contract: renders the shared FilePreviewer engine
 * for the attachment descriptor, with an "open in new tab" escape hatch
 * and a close button.
 *
 * Source-text contract tests — same pattern as ChatInput.inheritedBadge.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './FilePreviewPane.jsx'), 'utf8');

describe('FilePreviewPane.jsx', () => {
  it('renders the shared FilePreviewer engine for the file', () => {
    expect(SOURCE).toMatch(/import FilePreviewer from '\.\/FilePreviewer'/);
    expect(SOURCE).toMatch(/<FilePreviewer\s+url=\{file\.file_url\}\s+name=\{name\}\s+kind=\{file\.kind\}\s+fileType=\{file\.ext\}\s+className="h-full w-full object-contain"\s*\/>/);
  });

  it('fills the pane instead of relying on the modal-era default sizing', () => {
    // Regression: FilePreviewer's default iframe class is h-[70vh] (built
    // for the 85vh modal) — inside a right panel that left a dead gap
    // below a floating framed box. The pane must pass h-full w-full.
    expect(SOURCE).toMatch(/className="h-full w-full object-contain"/);
    expect(SOURCE).toMatch(/<FilePreviewer[\s\S]{0,300}?className="h-full w-full object-contain"/);
  });

  it('has an open-in-new-tab escape hatch', () => {
    expect(SOURCE).toMatch(/data-testid="file-preview-open-tab"/);
    expect(SOURCE).toMatch(/target="_blank"/);
  });

  it('has a close button wired to onClose', () => {
    expect(SOURCE).toMatch(/data-testid="file-preview-close"/);
    expect(SOURCE).toMatch(/onClick=\{onClose\}/);
  });

  it('shows the file name in the pane header', () => {
    expect(SOURCE).toMatch(/truncate text-sm font-medium text-foreground/);
    expect(SOURCE).toMatch(/title=\{name\}/);
  });
});
