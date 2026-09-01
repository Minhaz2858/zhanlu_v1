/**
 * Regression: the chat renderer must skip empty-content assistant
 * messages. These are backend "partial" messages left behind by the
 * v3 stream checkpoint — they have empty content and a tool_calls
 * array. Before the backend dedupe fix, they accumulated as multiple
 * assistant bubbles for a single user question ("I asked one
 * question and it's giving me multiple answers").
 *
 * Even after the backend dedupe, a small number of partials can
 * survive in the DB (e.g. when a turn was paused and never resumed,
 * the partial is the last assistant message). Filtering them at
 * render time is a belt-and-suspenders fix that ensures users never
 * see a wall of empty assistant bubbles.
 *
 * Why source-text tests (not React Testing Library)?
 * --------------------------------------------------
 * Same pattern as the other Chat / ChatInput tests in this repo:
 * no DOM, no router, no mocks. The contract is the filter predicate
 * in the JSX.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx renderer filters empty assistant partials', () => {
  it('the messages list filter skips assistant messages with empty content', () => {
    // The renderer at line ~2191 uses ``messages.filter(...)``. It
    // must skip messages that have role=assistant AND empty content.
    // The predicate should look like:
    //   !m.hidden && !(m.role === 'assistant' && !m.content)
    // or an equivalent helper.
    // Find the ``messages.filter(...)`` call (the visible-list build).
    // The predicate is ``(m) => !m.hidden && (m.id === streamingId ||
    // !(m.role === 'assistant' && !m.content))`` — since 2026-08-31 it
    // feeds a hoisted ``visible`` array (IIFE render block) instead of
    // an inline ``.map``, but the predicate contract is identical.
    const filterCall = SOURCE.match(
      /messages\.filter\(\(m\)\s*=>\s*([\s\S]{0,400}?)\)\s*;/,
    );
    expect(filterCall, 'messages.filter((m) => ...) pattern not found').not.toBeNull();
    const predicate = filterCall[1];
    // Must reference hidden, role=assistant, AND content emptiness
    expect(predicate).toMatch(/hidden/);
    expect(predicate).toMatch(/assistant/);
    expect(predicate).toMatch(/content/);
  });

  it('exempts the in-flight streaming placeholder from the empty-content filter', () => {
    // Regression (2026-08-05): while the v3 agent runs, the streaming
    // path appends an assistant placeholder with content:'' and sets
    // streamingId to its id. Activity steps (phase headlines, tool
    // calls, reasoning) are then rendered INTO that placeholder, so
    // its content stays empty until the LLM emits the final answer.
    // The empty-partial filter above would also drop this live bubble,
    // making the whole run invisible (blank chat) until completion —
    // users saw no "responding" feedback. The predicate must exempt
    // the message whose id === streamingId so the live bubble stays
    // visible while historical empty partials stay hidden.
    const filterCall = SOURCE.match(
      /messages\.filter\(\(m\)\s*=>\s*([\s\S]{0,400}?)\)\s*;/,
    );
    expect(filterCall, 'messages.filter((m) => ...) pattern not found').not.toBeNull();
    const predicate = filterCall[1];
    // Must short-circuit on the streaming id BEFORE the empty-content
    // check, so the live placeholder is always rendered.
    expect(predicate).toMatch(/streamingId/);
    // The OR must put the streamingId exemption first so the empty-
    // content check is never evaluated for the live placeholder.
    expect(predicate).toMatch(/m\.id\s*===\s*streamingId\s*\|\|\s*!/);
  });
});
