/**
 * PlusMenu.jsx upload-entry gating: the "Upload file" menu item (and its
 * hidden file input) must disappear when the parent passes
 * uploadEnabled={false} — i.e. when the user's file_upload_enabled
 * setting is off. The + menu itself (skills / agents / projects) stays
 * functional; only the upload affordance is removed.
 *
 * Source-text contract tests — same pattern as ChatInput.inheritedBadge.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './PlusMenu.jsx'), 'utf8');

describe('PlusMenu.jsx upload gating', () => {
  it('declares uploadEnabled with a default of true', () => {
    const props = SOURCE.match(/export default function PlusMenu\(\s*\{[\s\S]{0,400}?\}\s*\)/);
    expect(props, 'PlusMenu props destructure not found').not.toBeNull();
    expect(props[0]).toMatch(/uploadEnabled\s*=\s*true/);
  });

  it('hides the Upload file entry when uploadEnabled is false', () => {
    const mainView = SOURCE.match(/\{view === 'main'\s*\?\s*\([\s\S]{0,2500}?<\/div>\s*\)\s*:/);
    expect(mainView, 'main menu block not found').not.toBeNull();
    // The Upload button must be wrapped in an uploadEnabled gate.
    expect(mainView[0]).toMatch(/\{uploadEnabled\s*&&\s*\(\s*<button[\s\S]{0,600}?\}\s*<\/button>\s*\)\}/);
    expect(mainView[0]).toMatch(/t\.chat\.plus\.upload/);
  });

  it('hides the hidden file input when uploadEnabled is false', () => {
    expect(SOURCE).toMatch(/\{uploadEnabled\s*&&\s*\(\s*<input[\s\S]{0,400}?data-testid="file-upload-input"/);
  });

  it('still renders the + button itself regardless of uploadEnabled', () => {
    // The + menu must not disappear when upload is disabled — skills,
    // agents and projects are still selectable.
    expect(SOURCE).toMatch(/<Plus className="h-4 w-4" \/>/);
  });
});
