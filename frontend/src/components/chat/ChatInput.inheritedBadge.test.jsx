/**
 * Regression: ChatInput.jsx must surface a small "N data sources" badge
 * inside the project chip whenever the parent passes a positive
 * `inheritedKbCount`. The badge is the visible signal that the
 * project-context binding (data_source_runtime._extend_with_project_kbs)
 * is active — without it, users only find out the agent has project
 * resources by asking "what can you do?" and getting an answer that
 * mentions resources they thought belonged only to a project agent.
 *
 * Why source-text tests?
 * ----------------------
 * 1. These tests pin the *contract* (prop name, gate condition, render
 *    site) so a refactor that quietly drops the badge fails loudly.
 * 2. They run without booting React/router/mocks — same pattern as
 *    ChatInput.ungroupedChip.test.jsx.
 *
 * Failure mode this prevents
 * --------------------------
 * `general_assistant` in a project with bound data sources silently inherits
 * `aipdp_data_warehouse_prod` (the MySQL KB). The user only learned
 * about it by asking the agent "what can you do for me?" — and was
 * surprised. This test pins the badge so the surprise goes away.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ChatInput.jsx'), 'utf8');

describe('ChatInput.jsx inherited data sources badge', () => {
  it('declares inheritedKbCount as a prop', () => {
    // The destructured props list must include `inheritedKbCount`
    // (default 0) so the component is decoupled from the parent's
    // fetch logic and can be tested in isolation.
    const propList = SOURCE.match(/function\s+ChatInput\s*\(\s*\{[\s\S]{0,800}?\}\s*\)/);
    expect(propList, 'ChatInput props destructure not found').not.toBeNull();
    expect(propList[0]).toMatch(/inheritedKbCount/);
  });

  it('imports the Database icon from lucide-react', () => {
    // No emojis — must use the lucide-react `Database` icon for the
    // data-sources indicator.
    expect(SOURCE).toMatch(
      /import\s*\{[^}]*\bDatabase\b[^}]*\}\s*from\s*['"`]lucide-react['"`]/,
    );
  });

  it('renders the badge inside the project chip block', () => {
    // The badge must live inside the same JSX block as the project
    // chip (so it shares the chip's amber styling and clear button),
    // NOT as a sibling chip after it. The block is delimited by
    // ``{pendingProject && ... ( <span ...> ... </span> )}`` — find
    // it by matching the outer span's class + close.
    const chipBlock = SOURCE.match(
      /\{pendingProject\s*&&\s*!isUngroupedProjectName\([\s\S]{0,2500}?<\/span>\s*\)\}/,
    );
    expect(chipBlock, 'project chip block not found').not.toBeNull();
    // The badge gate must reference inheritedKbCount > 0 (so it
    // disappears cleanly when the project has no KBs).
    expect(chipBlock[0]).toMatch(/inheritedKbCount\s*>\s*0/);
    // And it must render the numeric count.
    expect(chipBlock[0]).toMatch(/\{inheritedKbCount\}/);
  });

  it('badge has a hover tooltip explaining project inheritance', () => {
    // The badge should make the *project context* visible. A short
    // title attribute (zh + en) is enough — the user needs to know
    // *why* the agent can talk to a database they didn't bind.
    const chipBlock = SOURCE.match(
      /\{pendingProject\s*&&\s*!isUngroupedProjectName\([\s\S]{0,2500}?<\/span>\s*\)\}/,
    );
    expect(chipBlock, 'project chip block not found').not.toBeNull();
    expect(chipBlock[0]).toMatch(/title=/);
    // The English tooltip must mention "data source" or similar so
    // an English-speaking user can tell what the number means.
    expect(chipBlock[0]).toMatch(/[Dd]ata\s*[Ss]ource/);
  });
});
