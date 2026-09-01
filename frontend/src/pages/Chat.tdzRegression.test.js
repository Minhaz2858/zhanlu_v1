/**
 * Regression test for the TDZ bug in handleAgentSend (Chat.jsx).
 *
 * Bug: inside `handleAgentSend`, the code had:
 *
 *   try {
 *     stream.startSending();                  // ← TDZ ReferenceError
 *     const stream = streamAgentResponse(...) // ← shadows outer `stream`
 *     ...
 *   } catch (streamErr) { ... }
 *
 * The inner `const stream` shadows the outer `stream` (from
 * `usePersistentStream()`), creating a Temporal Dead Zone. The
 * `stream.startSending()` call on the previous line throws
 * `ReferenceError: Cannot access 'stream' before initialization`.
 * The catch block treats it as a generic stream failure and writes
 * "Sorry, the connection was interrupted. Please try again." to the
 * assistant ChatMessage row.
 *
 * The fix renames the inner variable to `streamGen` so the outer
 * `stream` stays accessible throughout the function.
 *
 * This test verifies that the source code no longer contains the
 * shadowing pattern that causes the TDZ.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const CHAT_JSX = resolve(__dirname, 'Chat.jsx');

describe('Chat.jsx TDZ regression', () => {
  it('does not shadow `stream` with `const stream =` inside handleAgentSend', () => {
    const src = readFileSync(CHAT_JSX, 'utf8');
    // The buggy pattern: `const stream = streamAgentResponse(` inside a
    // function that already has access to an outer `stream` variable.
    // After the fix, the inner variable is named `streamGen`.
    expect(src).not.toContain('const stream = streamAgentResponse(');
    expect(src).toContain('const streamGen = streamAgentResponse(');
  });

  it('the for-await loop iterates `streamGen`, not `stream`', () => {
    const src = readFileSync(CHAT_JSX, 'utf8');
    // After the fix, the for-await loop should use `streamGen`.
    expect(src).toContain('for await (const evt of streamGen)');
    // And should NOT have `for await (const evt of stream)` inside
    // handleAgentSend (that would mean the shadowing is still there).
    // Note: `for await (const evt of stream)` may appear in OTHER
    // functions (e.g. legacy handleSend) where there's no shadowing —
    // that's fine. We just check the handleAgentSend region.
    const handleAgentSendStart = src.indexOf('async function handleAgentSend');
    const handleAgentSendEnd = src.indexOf('async function handleClear');
    const handleAgentSendBody = src.slice(handleAgentSendStart, handleAgentSendEnd);
    expect(handleAgentSendBody).not.toContain('for await (const evt of stream)');
    expect(handleAgentSendBody).toContain('for await (const evt of streamGen)');
  });
});
