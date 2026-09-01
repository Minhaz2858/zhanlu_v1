/**
 * Regression: translations.js must define the new i18n keys used by the
 * Skill Agent / Agent Builder wait-for-input guided flow (see spec
 * 2026-07-28-skill-agent-wait-for-input-design.md).
 *
 * The keys are: prefilledHint, or, emptyUploadCta, emptyUploadHint,
 * existingSkills, backToToolkit (skillAgent) and prefilledHint, or,
 * existingAgents, backToAgents (agentBuilder) — in BOTH zh and en.
 *
 * This test pins the contract so a future refactor that drops a key
 * (or only adds it to one language) will fail.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './translations.js'), 'utf8');

const SKILL_AGENT_KEYS = [
  'prefilledHint', 'or', 'emptyUploadCta', 'emptyUploadHint',
  'existingSkills', 'backToToolkit', 'scrape',
];

function expectKey(block, key, label) {
  if (key === 'scrape') {
    expect(block, `missing ${label} skillAgent key: ${key}`).toMatch(
      new RegExp(key + ':\\s*\\{'),
    );
    return;
  }
  expect(block, `missing ${label} skillAgent key: ${key}`).toMatch(
    new RegExp(key + ":\\s*['\"]"),
  );
}
const AGENT_BUILDER_KEYS = [
  'prefilledHint', 'or', 'existingAgents', 'backToAgents',
];

/**
 * Find the Nth occurrence (1-indexed) of a top-level `key:` block in the
 * source. We use this to distinguish the zh block (1st occurrence) from
 * the en block (2nd occurrence).
 */
function nthBlock(source, key, n) {
  const re = new RegExp('^    ' + key + ':\\s*\\{', 'gm');
  let match;
  let count = 0;
  while ((match = re.exec(source)) !== null) {
    count++;
    if (count === n) {
      // Slice a generous window so all keys in the block are captured.
      return source.slice(match.index, match.index + 3000);
    }
  }
  return null;
}

describe('translations.js — skillAgent / agentBuilder new keys', () => {
  it('zh skillAgent block has all new keys', () => {
    const block = nthBlock(SOURCE, 'skillAgent', 1);
    expect(block, 'zh skillAgent block not found').not.toBeNull();
    for (const k of SKILL_AGENT_KEYS) {
      expectKey(block, k, 'zh');
    }
  });

  it('zh agentBuilder block has all new keys', () => {
    const block = nthBlock(SOURCE, 'agentBuilder', 1);
    expect(block, 'zh agentBuilder block not found').not.toBeNull();
    for (const k of AGENT_BUILDER_KEYS) {
      expect(block, 'missing zh agentBuilder key: ' + k).toMatch(
        new RegExp(k + ":\\s*['\"]"),
      );
    }
  });

  it('en skillAgent block has all new keys', () => {
    const block = nthBlock(SOURCE, 'skillAgent', 2);
    expect(block, 'en skillAgent block not found').not.toBeNull();
    for (const k of SKILL_AGENT_KEYS) {
      expectKey(block, k, 'en');
    }
  });

  it('en agentBuilder block has all new keys', () => {
    const block = nthBlock(SOURCE, 'agentBuilder', 2);
    expect(block, 'en agentBuilder block not found').not.toBeNull();
    for (const k of AGENT_BUILDER_KEYS) {
      expect(block, 'missing en agentBuilder key: ' + k).toMatch(
        new RegExp(k + ":\\s*['\"]"),
      );
    }
  });
});
