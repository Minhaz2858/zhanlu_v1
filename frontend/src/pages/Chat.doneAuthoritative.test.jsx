/**
 * Contract test — the `done` event's server content is AUTHORITATIVE.
 *
 * Live failure (2026-10-19, msg 4734e09d): "give me last week sales overview"
 * produced the full report TWICE in the persisted message. The server side
 * was correct (done content = 4,994 chars, post-dedupe join of all iteration
 * prose), but Chat.jsx's done handler used "prefer the LONGER of (local
 * deltas, server content)" — a client-side double append of an earlier
 * iteration's deltas won, and the final ChatMessage.update PUT wrote the
 * 9,949-char doubled copy over the server's correct row.
 *
 * Contract: the done handler must TAKE server content whenever it is
 * non-empty (trim()), keeping local accumulation only as a degenerate-stream
 * fallback. Same contract on MobileChatPage.jsx.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/** Slice the source starting at the done-branch marker (~1200 chars covers
 *  the handler body through the setMessages call). */
function doneBlock(source) {
  const idx = source.indexOf("evt.type === 'done'");
  if (idx < 0) return null;
  return source.slice(idx, idx + 1400);
}

describe('Chat.jsx done handler — server content is authoritative', () => {
  const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');
  const block = doneBlock(SOURCE);

  it('done branch exists', () => {
    expect(block, 'done branch not found').not.toBeNull();
  });

  it('takes server done content whenever it is non-empty', () => {
    expect(block).toMatch(
      /typeof evt\.content === 'string' && evt\.content\.trim\(\)\) \{\s*\n\s*fullContent = evt\.content;/,
    );
  });

  it('no longer prefers the LONGER of (local, server) — the doubling bug', () => {
    expect(block).not.toMatch(/\.length > \(fullContent/);
    expect(block).not.toMatch(/LONGER of/);
  });

  it('server-wins rationale is documented at the merge site', () => {
    expect(block).toMatch(/post-dedupe/);
    expect(block).toMatch(/authoritative/i);
  });

  it('final ChatMessage.update persists fullContent (unchanged contract)', () => {
    expect(SOURCE).toMatch(
      /entities\.ChatMessage\.update\(aiMsg\.id, \{\s*\n\s*content: fullContent,/,
    );
  });
});

describe('MobileChatPage.jsx done handler — same contract', () => {
  const SOURCE = readFileSync(
    resolve(__dirname, './mobile/MobileChatPage.jsx'),
    'utf8',
  );
  const block = doneBlock(SOURCE);

  it('takes server done content whenever it is non-empty', () => {
    expect(block, 'done branch not found').not.toBeNull();
    expect(block).toMatch(
      /typeof evt\.content === 'string' && evt\.content\.trim\(\)\) \{\s*\n\s*fullContent = evt\.content;/,
    );
  });

  it('no longer prefers the LONGER merge', () => {
    expect(block).not.toMatch(/\.length > \(fullContent/);
  });
});
