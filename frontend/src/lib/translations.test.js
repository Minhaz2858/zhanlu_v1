/**
 * Regression: `en.skillAgent.suggestions.create` must not use hypothetical
 * "e.g." phrasing (2026-07-28).
 *
 * Root cause: the original chip said
 *     'Create a new skill, e.g. a report generation tool'
 * The Skill Agent LLM read "e.g." as a hypothetical marker and treated
 * the chip as a non-binding example rather than a directive. The user
 * clicked the chip and the agent replied "I'll stop the skill creation
 * there" without ever calling `create_skill`.
 *
 * Fix: rewrite the chip to drop "e.g." and use a colon instead, so the
 * example reads as a concrete template the agent should follow.
 *
 * This test pins the contract: any future change that re-introduces
 * "e.g." (or similar hypothetical markers) in this specific chip will
 * fail loudly. The other "e.g." chips in the same file (Agent Builder
 * `create` / `fromTemplate`, Automation `create` / `fromTemplate`) are
 * intentionally left alone — the user explicitly approved narrowing the
 * fix to just the Skill Agent chip in the 2026-07-28 brainstorming
 * session.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './translations.js'), 'utf8');

/**
 * Find the Skill Agent `create` chip in the en block of translations.js.
 *
 * Strategy: locate the `en:` language block by anchoring to the
 * top-level `en: {` header, then within that block locate the
 * `skillAgent:` object and the `suggestions.create:` chip. We use a
 * brace-counting approach that respects string literals (single,
 * double, and backtick) so it works even when chip text contains
 * `{` / `}` (it currently does not, but we want to be safe).
 */
function extractSkillAgentCreateChip() {
  // Find all `en:` blocks. The file is structured as
  //   export default { zh: {...}, en: {...}, ... };
  // Each language block starts with `  en: {` (2 spaces + key + colon + space + brace).
  // We pick the LAST one to be safe in case there are multiple.
  const enHeaderRe = /^( {2})(\w+):\s*\{/gm;
  const enHeaders = [];
  let m;
  while ((m = enHeaderRe.exec(SOURCE)) !== null) {
    enHeaders.push({ key: m[2], index: m.index, indent: m[1].length });
  }
  // Filter to the language blocks (top-level). Use the indent of the
  // first match as the canonical indent.
  if (enHeaders.length === 0) {
    throw new Error('No top-level language blocks found in translations.js');
  }
  const topIndent = enHeaders[0].indent;
  const langBlocks = enHeaders.filter((h) => h.indent === topIndent);
  const enBlock = langBlocks.find((b) => b.key === 'en');
  if (!enBlock) {
    throw new Error('No "en" language block found in translations.js');
  }

  // Brace-count from the start of the en block, respecting strings.
  const enStart = enBlock.index + `    en: {`.length;
  const enBody = readBalancedBlock(SOURCE, enStart);
  if (!enBody) {
    throw new Error('Failed to balance braces for the en block');
  }

  // Now find skillAgent.create within enBody. The chip line is at
  // indent 8 (2 levels deep): `        create: '...'`.
  // We match by content: the chip should contain "report generation tool"
  // (the unique Skill Agent template).
  const createLineRe = /^ {8}create:\s*['"`]([^'"`]+)['"`]/gm;
  while ((m = createLineRe.exec(enBody)) !== null) {
    if (m[1].includes('report generation tool')) {
      return m[1];
    }
  }
  throw new Error(
    "Could not find Skill Agent 'create' chip ('report generation tool' template) in the en block of translations.js"
  );
}

/**
 * Read a balanced `{...}` block from `source` starting at index `start`.
 * Respects single, double, and backtick string literals (and template
 * literals). Returns the content INSIDE the braces, or null if unbalanced.
 */
function readBalancedBlock(source, start) {
  let depth = 1;
  let i = start;
  let inStr = null; // '"' | "'" | '`' | null
  let escaped = false;
  while (i < source.length && depth > 0) {
    const ch = source[i];
    if (inStr) {
      if (escaped) {
        escaped = false;
      } else if (ch === '\\') {
        escaped = true;
      } else if (ch === inStr) {
        inStr = null;
      }
    } else {
      if (ch === '"' || ch === "'" || ch === '`') {
        inStr = ch;
      } else if (ch === '{') {
        depth++;
      } else if (ch === '}') {
        depth--;
        if (depth === 0) {
          return source.slice(start, i);
        }
      }
    }
    i++;
  }
  return null;
}

describe('en.skillAgent.suggestions.create (Skill Agent empty-state chip)', () => {
  const chip = extractSkillAgentCreateChip();

  it('does not contain "e.g." hypothetical marker', () => {
    expect(chip.toLowerCase()).not.toContain('e.g.');
  });

  it('does not contain "i.e.", "for example", or "such as"', () => {
    const lower = chip.toLowerCase();
    // These are stronger hypothetical markers. The Skill Agent create
    // chip must be a direct directive, not a hypothetical example.
    const forbidden = ['e.g.', 'i.e.', 'for example', 'such as'];
    for (const marker of forbidden) {
      expect(lower).not.toContain(marker);
    }
  });

  it('starts with a directive verb ("Create")', () => {
    // The chip must read as a directive to the agent, not as a
    // hypothetical example. We check it starts with a verb that
    // matches the create-skill action.
    expect(chip).toMatch(/^Create\b/i);
  });

  it('preserves the "report generation tool" template body', () => {
    // The 2026-07-28 fix replaced "e.g." with ":" — the rest of the
    // chip should stay the same. Pin the template body so a future
    // rewrite can't silently drop the concrete example either.
    expect(chip).toContain('a report generation tool');
  });
});
