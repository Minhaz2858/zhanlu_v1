/**
 * Contract tests for the Kimi/GPT-style chat UX: Regenerate button +
 * data-source citation chips (2026-08-31).
 *
 * Same pattern as Chat.v3StreamProjectContext / Chat.projectContextLeak:
 * no DOM, no router, no mocks. The contracts live in the JSX source:
 *   1. handleRegenerate reuses the SAME assistant bubble (same id) and
 *      calls handleAgentSend with `regenerate: true`.
 *   2. The stream body carries `regenerate: true` when set.
 *   3. The `done` handler ASSIGNS the hoisted `sources` accumulator
 *      (NOT a shadowing `let` inside the block) so the final
 *      ChatMessage.update can persist citations — the ReferenceError
 *      regression guard.
 *   4. The persist block writes `sources` to the ChatMessage row.
 *   5. MessageBubble receives onRegenerate ONLY on the last visible
 *      assistant message while idle.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

// Capture the body of `async function <name>(...) { ... }` at component
// top level (closing brace is exactly two-space indented).
function captureFunction(name) {
  const pattern = new RegExp(
    'async\\s+function\\s+' + name + '\\([\\s\\S]*?\\)\\s*\\{([\\s\\S]*?)\\n  \\}\\n',
  );
  const match = SOURCE.match(pattern);
  return match ? match[1] : null;
}

describe('Chat.jsx regenerate (Kimi/GPT-style)', () => {
  it('handleRegenerate exists and reuses the SAME assistant message id', () => {
    const fn = captureFunction('handleRegenerate');
    expect(fn, 'handleRegenerate not found').not.toBeNull();
    // Reuse targetMsg (same id) so the stream merges into the existing
    // bubble and the final ChatMessage.update rewrites the same row.
    expect(fn).toMatch(/targetMsg\.id/);
    expect(fn).toMatch(/setMessages/);
  });

  it('handleRegenerate calls handleAgentSend with regenerate: true', () => {
    const fn = captureFunction('handleRegenerate');
    expect(fn).not.toBeNull();
    expect(fn).toMatch(/handleAgentSend\(/);
    expect(fn).toMatch(/regenerate:\s*true/);
  });

  it('handleRegenerate refuses to run while streaming or loading', () => {
    const fn = captureFunction('handleRegenerate');
    expect(fn).not.toBeNull();
    expect(fn).toMatch(/streamingId\s*\|\|\s*loading/);
  });

  it('stream body carries regenerate flag when set', () => {
    expect(SOURCE).toMatch(/\.\.\.\(regenerate \? \{ regenerate: true \} : \{\}\)/);
  });

  it('hoists sources accumulator OUTSIDE the done block (ReferenceError guard)', () => {
    // Bug (2026-08-31): `let sources = []` was declared inside the
    // `evt.type === 'done'` block but referenced in the outer persist
    // scope → ReferenceError skipped the ChatMessage.update every turn.
    const hoist = SOURCE.match(/let finalArtifacts = \[\];[\s\S]*?let sources = \[\];/);
    expect(hoist, 'hoisted sources accumulator not found').not.toBeNull();
    // The done block must ASSIGN to the outer variable — no shadowing `let`.
    const doneBlock = SOURCE.match(/evt\.type === 'done'([\s\S]*?)\n        }\n      }\n    }\n    \/\/ Defensive:/);
    if (doneBlock) {
      expect(doneBlock[1]).not.toMatch(/let sources = \[\];/);
      expect(doneBlock[1]).toMatch(/sources = lastAssistant\.sources/);
    }
  });

  it('persists sources in the final ChatMessage.update', () => {
    expect(SOURCE).toMatch(/\.\.\.\(sources\.length \? \{ sources \} : \{\}\)/);
  });

  it('passes onRegenerate only on the last visible assistant message while idle', () => {
    const invocation = SOURCE.match(/onRegenerate=\{canRegenerate \? handleRegenerate : null\}/);
    expect(invocation, 'onRegenerate prop not wired into MessageBubble').not.toBeNull();
    // canRegenerate = last visible assistant message + idle + has content.
    expect(SOURCE).toMatch(
      /canRegenerate = m\.role === 'assistant' && !!m\.content && m\.id === lastId && idle && !isStreaming/,
    );
    expect(SOURCE).toMatch(/const lastId = visible\.length \? visible\[visible\.length - 1\]\.id : null/);
  });
});
