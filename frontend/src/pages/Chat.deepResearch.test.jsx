/**
 * Contract tests for Chat.jsx deep-research plumbing (v1).
 *
 * Pins that the composer's research config travels from `handleSend`
 * opts through `handleAgentSend` into the v3 stream body — the backend
 * only runs the research protocol when the message carries
 * `research: {enabled, depth, scope}`.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx deep research plumbing', () => {
  it('holds research state passed to the composer', () => {
    expect(SOURCE).toMatch(/researchConfig/);
    expect(SOURCE).toMatch(/onResearchChange/);
  });

  it('forwards research opts from handleSend into handleAgentSend', () => {
    // handleSend(text, opts) must destructure research and pass it on.
    expect(SOURCE).toMatch(/const\s*\{\s*[^}]*\bresearch\b[^}]*\}\s*=\s*opts/);
  });

  it('includes research metadata in the v3 stream body', () => {
    // The streamAgentResponse body must spread research when present.
    expect(SOURCE).toMatch(/\.\.\.\(\s*research\s*\?\s*\{\s*research\s*\}\s*:\s*\{\}\s*\)/);
  });
});
