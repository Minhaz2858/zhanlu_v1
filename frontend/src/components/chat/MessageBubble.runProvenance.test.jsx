/**
 * Phase 8.1 Stage 1 — RunProvenancePanel integration into the chat bubble.
 *
 * MessageBubble is the chat message renderer (used by Chat.jsx for every
 * assistant turn). The panel is wired there so any message carrying a
 * backend-attached ``run_id`` (see backend
 * tests/test_chat_turn_run_linkage.py) shows its provenance inline.
 * MessageBubble is foreign-dirty and huge — this test follows the repo's
 * source-anchored convention for that file.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(
  resolve(__dirname, './MessageBubble.jsx'), 'utf8');

describe('MessageBubble provenance integration (Phase 8.1 Stage 1)', () => {
  it('imports RunProvenancePanel', () => {
    expect(SOURCE).toMatch(
      /import\s+RunProvenancePanel\s+from\s+['"].\/RunProvenancePanel['"]/);
  });

  it('renders the panel only for non-streaming messages that carry a run_id', () => {
    // gated on run_id — no fabrication for historical turns without one
    expect(SOURCE).toMatch(
      /!\s*isStreaming\s*&&\s*message\.run_id\s*&&\s*\(\s*<RunProvenancePanel/);
  });

  it('passes the message run_id to the panel', () => {
    expect(SOURCE).toMatch(
      /<RunProvenancePanel[^>]*runId=\{message\.run_id\}/);
  });
});
