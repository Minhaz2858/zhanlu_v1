/**
 * Attachment persistence on user messages (both desktop Chat.jsx and
 * mobile MobileChatPage.jsx): when the user sends a message with attached
 * files, the user ChatMessage row must carry the attachment descriptors so
 * the file cards render in chat history and survive a page refresh.
 *
 * Source-text contract tests — same pattern as ChatInput.inheritedBadge.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const CHAT = readFileSync(resolve(__dirname, '../../pages/Chat.jsx'), 'utf8');
const MOBILE = readFileSync(resolve(__dirname, '../../pages/mobile/MobileChatPage.jsx'), 'utf8');

describe('Chat.jsx user-message attachment persistence', () => {
  it('writes attachments on the user ChatMessage.create', () => {
    const createBlock = CHAT.match(
      /const userMsg = await base44\.entities\.ChatMessage\.create\(\{[\s\S]{0,900}?\}\);/,
    );
    expect(createBlock, 'user ChatMessage.create not found').not.toBeNull();
    expect(createBlock[0]).toMatch(/attachments\.length\s*\?/);
    expect(createBlock[0]).toMatch(/attachments:\s*attachments\.map\(\(a\)\s*=>\s*\(\{ name: a\.name, file_url: a\.file_url, ext: a\.ext, kind: a\.kind \}\)/);
  });
});

describe('MobileChatPage.jsx user-message attachment persistence', () => {
  it('writes attachments on the user ChatMessage.create', () => {
    const createBlock = MOBILE.match(
      /const userMsg = await base44\.entities\.ChatMessage\.create\(\{[\s\S]{0,900}?\}\);/,
    );
    expect(createBlock, 'user ChatMessage.create not found').not.toBeNull();
    expect(createBlock[0]).toMatch(/attachments\.length\s*\?/);
    expect(createBlock[0]).toMatch(/attachments:\s*attachments\.map/);
  });
});
