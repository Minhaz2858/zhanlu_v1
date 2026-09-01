/**
 * Regression: When the Skill Agent creates (or updates) a skill via the
 * create_skill / update_skill tool calls, the right-side file panel
 * (`<SkillFilePanel skill={currentSkill} />`) must update to show the
 * newly created / refreshed skill's files (SKILL.md, _meta.json, refs).
 *
 * Previously, `currentSkill` was only ever set via `startWithSkill(skill)`
 * (triggered by ?skill={id}), so skills created in-chat showed the empty
 * "skill-workspace" placeholder on the right. See user bug report 2026-07-28:
 * "skills created but it not showing on the right side".
 *
 * The fix: a useEffect that watches `messages`, scans tool_calls for
 * completed create_skill / update_skill entries, parses `tc.results`
 * (handles JSON string), and calls setCurrentSkill with the skill dict.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './SkillAgent.jsx'), 'utf8');

describe('SkillAgent.jsx — currentSkill sync from create_skill/update_skill tool results', () => {
  it('declares a useEffect that depends on messages', () => {
    // Look for a useEffect whose deps array includes messages — this is
    // where the sync logic lives.
    expect(SOURCE).toMatch(/useEffect\(\s*\(\s*\)\s*=>\s*\{/);
    expect(SOURCE).toMatch(/,\s*\[messages\]\s*\)/);
  });

  it('the effect iterates messages (reverse) looking for tool_calls', () => {
    // Either a numeric reverse-iteration (for (let i = N - 1; ...)) or a
    // reversed array + for-of. Both are valid patterns; we accept either.
    const numReverse = SOURCE.match(/for\s*\(\s*let\s+i\s*=\s*messages\.length\s*-\s*1\s*;\s*i\s*>=\s*0\s*;\s*i--\s*\)/);
    const reversedForOf = SOURCE.match(/\[\.\.\.messages\]\.reverse\(\)|messages\.slice\(\)\.reverse\(\)/);
    expect(numReverse || reversedForOf, 'no reverse iteration over messages found').not.toBeNull();
    expect(SOURCE).toMatch(/tool_calls\s*\|\|\s*\[\s*\]/);
  });

  it('filters tool calls by status (completed/success)', () => {
    expect(SOURCE).toMatch(/tc\.status\s*\|\|\s*['"`]['"`]/);
    expect(SOURCE).toMatch(/['"`]completed['"`]\s*\|\|\s*['"`].*?\.includes|includes\(.*tc\.status/);
  });

  it('filters tool calls by name (LLM-facing OR wire-format display name)', () => {
    // The backend's TOOL_DISPLAY_NAMES map at
    // backend/app/routers/agents.py:1124-1128 translates the LLM-facing
    // names (create_skill / update_skill) to the wire-format display
    // names (Tool.create / Tool.update) that the frontend actually
    // receives in tc.name. The sync must accept BOTH — otherwise a
    // freshly created skill never lights up the file panel.
    expect(SOURCE).toMatch(/create_skill/);
    expect(SOURCE).toMatch(/update_skill/);
    expect(SOURCE).toMatch(/Tool\.create/);
    expect(SOURCE).toMatch(/Tool\.update/);
    // The check should reference tc.name (directly or via a derived
    // value like tcName = String(tc.name || '')).
    const usesTcName =
      /tc\.name\s*===/.test(SOURCE) ||
      /\btcName\s*=/.test(SOURCE) ||
      /\btcName\s*===/.test(SOURCE);
    expect(usesTcName, 'expected the filter to use tc.name (directly or via tcName)').toBe(true);
  });

  it('parses tc.results — handles both JSON string and object', () => {
    expect(SOURCE).toMatch(/typeof\s+\w+\s*===\s*['"`]string['"`]/);
    expect(SOURCE).toMatch(/JSON\.parse/);
  });

  it('sets currentSkill via setCurrentSkill when a valid skill result is found', () => {
    expect(SOURCE).toMatch(/setCurrentSkill\s*\(\s*\w+\s*\)/);
  });

  it('still preserves startWithSkill as the entry-point path', () => {
    expect(SOURCE).toMatch(/startWithSkill\(skillId\)/);
  });
});
