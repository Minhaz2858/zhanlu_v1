/**
 * Chat.jsx right-side file preview pane wiring: clicking an attachment
 * card in chat history must open the FilePreviewPane in a resizable right
 * panel (not a new tab), and the pane must close when switching sessions.
 *
 * Source-text contract tests — same pattern as ChatInput.inheritedBadge.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, '../../pages/Chat.jsx'), 'utf8');

describe('Chat.jsx file preview pane wiring', () => {
  it('imports FilePreviewPane', () => {
    expect(SOURCE).toMatch(/import FilePreviewPane from '@\/components\/chat\/FilePreviewPane';/);
  });

  it('holds the pane state and passes onPreviewFile to MessageBubble', () => {
    expect(SOURCE).toMatch(/const \[openFilePreview, setOpenFilePreview\] = useState\(null\);/);
    expect(SOURCE).toMatch(/onPreviewFile=\{setOpenFilePreview\}/);
  });

  it('renders the pane in a right-side resizable Panel', () => {
    const panelBlock = SOURCE.match(
      /\{openFilePreview\s*&&\s*\(\s*<>\s*<PanelResizeHandle[\s\S]{0,600}?<FilePreviewPane[\s\S]{0,200}?<\/Panel>\s*<\/>\s*\)\}/,
    );
    expect(panelBlock, 'file preview panel block not found').not.toBeNull();
    expect(panelBlock[0]).toMatch(/id="file-preview-panel"/);
    expect(panelBlock[0]).toMatch(/onClose=\{\(\) => setOpenFilePreview\(null\)\}/);
  });

  it('closes the pane when switching sessions', () => {
    expect(SOURCE).toMatch(/setOpenFilePreview\(null\);/);
  });
});
