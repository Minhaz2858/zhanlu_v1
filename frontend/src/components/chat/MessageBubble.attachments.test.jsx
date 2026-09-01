/**
 * MessageBubble.jsx attachment cards in chat history (Kimi/ChatGPT-style):
 * a user message carrying `message.attachments` must render a clickable
 * file card above the text, so the user can see which file the message
 * carried and open it — the missing piece the user reported ("file is not
 * showing in chat history").
 *
 * Source-text contract tests — same pattern as ChatInput.inheritedBadge.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './MessageBubble.jsx'), 'utf8');

describe('MessageBubble.jsx attachment cards', () => {
  it('reads message.attachments for the chip list', () => {
    expect(SOURCE).toMatch(/Array\.isArray\(message\.attachments\)/);
  });

  it('has an icon picker covering sheets, docs and images', () => {
    expect(SOURCE).toMatch(/function attachmentIcon/);
    expect(SOURCE).toMatch(/FileSpreadsheet/);
    expect(SOURCE).toMatch(/FileText/);
    expect(SOURCE).toMatch(/ImageIcon/);
  });

  it('renders a clickable card per attachment above the text', () => {
    const chipBlock = SOURCE.match(
      /\{attachmentList\.length\s*>\s*0\s*&&\s*\([\s\S]{0,2200}?data-testid="msg-attachment-chip"[\s\S]{0,800}?<\/a>[\s\S]{0,200}?\)\}/,
    );
    expect(chipBlock, 'attachment chip block not found').not.toBeNull();
    // Each card keeps a real href (fallback) and shows the file name.
    expect(chipBlock[0]).toMatch(/href=\{att\?\.file_url\}/);
    expect(chipBlock[0]).toMatch(/attName/);
  });

  it('opens the right preview pane instead of a new tab when onPreviewFile is provided', () => {
    const chipBlock = SOURCE.match(
      /\{attachmentList\.length\s*>\s*0\s*&&\s*\([\s\S]{0,2200}?data-testid="msg-attachment-chip"[\s\S]{0,800}?<\/a>[\s\S]{0,200}?\)\}/,
    );
    expect(chipBlock, 'attachment chip block not found').not.toBeNull();
    // Click intercepts: preventDefault + call onPreviewFile(att) so the
    // file renders in the chat's right-side pane (Kimi/ChatGPT-style).
    expect(chipBlock[0]).toMatch(/if\s*\(onPreviewFile\s*&&\s*att\?\.file_url\)\s*\{/);
    expect(chipBlock[0]).toMatch(/e\.preventDefault\(\)/);
    expect(chipBlock[0]).toMatch(/onPreviewFile\(att\)/);
  });

  it('declares onPreviewFile as a prop', () => {
    expect(SOURCE).toMatch(/onRegenerate,\s*onPreviewFile\s*\}/);
  });

  it('renders the cards inside the USER bubble (above the message text)', () => {
    // The chips must live inside the user-bubble div (bg-secondary rounded
    // card) and appear before the expanded/preview text branch.
    const bubble = SOURCE.match(
      /className="max-w-full rounded-2xl rounded-tr-sm bg-secondary px-4 py-2\.5 text-sm text-foreground"[\s\S]{0,3000}?\{userExpanded \?/,
    );
    expect(bubble, 'user bubble block not found').not.toBeNull();
    expect(bubble[0]).toMatch(/attachmentList\.length\s*>\s*0/);
  });
});
