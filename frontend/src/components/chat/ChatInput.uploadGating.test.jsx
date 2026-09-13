/**
 * ChatInput.jsx upload gating: all upload affordances (PlusMenu entry,
 * drag-drop, paste-image) are gated on the per-user file_upload_enabled
 * setting — the same setting the backend enforces with a 403.
 *
 * Note: the paperclip attach button was intentionally NOT added — upload
 * entry stays in the + menu only (user preference), so this suite pins
 * the gating that remains.
 *
 * Why source-text tests?
 * ----------------------
 * Same pattern as ChatInput.inheritedBadge.test.jsx: they pin the
 * contract (imports, gates) and run without booting React.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ChatInput.jsx'), 'utf8');

describe('ChatInput.jsx file_upload_enabled gating', () => {
  it('reads the per-user UserSetting row on mount', () => {
    expect(SOURCE).toMatch(/base44\.entities\.UserSetting\.list\('',\s*1\)/);
    // Default true while the fetch is in flight; flips to false only when
    // the row explicitly says false (null / missing row = enabled).
    expect(SOURCE).toMatch(/setUploadEnabled\(!\(rows\?\.\[0\]\?\.file_upload_enabled\s*===\s*false\)\)/);
  });

  it('drag-drop refuses files when upload is disabled', () => {
    const dropFn = SOURCE.match(/async function handleDrop\(e\) \{[\s\S]{0,900}?\n  \}/);
    expect(dropFn, 'handleDrop not found').not.toBeNull();
    expect(dropFn[0]).toMatch(/if\s*\(!uploadEnabled\)/);
    expect(dropFn[0]).toMatch(/File upload is disabled in Settings/);
  });

  it('paste-image is skipped when upload is disabled', () => {
    expect(SOURCE).toMatch(/if\s*\(disabled\s*\|\|\s*!uploadEnabled\)\s*return;/);
  });

  it('passes uploadEnabled to the PlusMenu so its entry hides too', () => {
    expect(SOURCE).toMatch(/<PlusMenu[\s\S]{0,500}?uploadEnabled=\{uploadEnabled\}/);
  });

  it('does NOT add a paperclip attach button (upload stays in the + menu)', () => {
    // User preference: no paperclip. The + menu is the single upload entry.
    expect(SOURCE).not.toMatch(/Paperclip/);
    expect(SOURCE).not.toMatch(/attach-button/);
  });
});
