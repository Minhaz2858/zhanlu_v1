/**
 * Regression: ProjectDetail.jsx must not use the legacy
 * ``{project: legacyName}`` fallback for AgentConversation.
 *
 * Why this exists
 * ---------------
 * A user reported that the "Recent Chats" section on the Project
 * Detail page showed every conversation in the DB, not just the
 * ones bound to the project. The cause was a legacy
 * ``mergeFbk(primary, {project: legacyName})`` call in loadAll():
 * the AgentConversation model has only ``project_id`` (FK) — no
 * ``project`` string column — so the backend's parse_query silently
 * drops the unknown field, the fallback returned ALL rows, and
 * mergeFbk unioned them into the project-scoped list.
 *
 * The other entities (KnowledgeBase, AutomationTask) still have
 * both columns so their fallback is correct and is kept. This test
 * pins the AgentConversation-specific constraint.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ProjectDetail.jsx'), 'utf8');

describe('ProjectDetail.jsx AgentConversation filter', () => {
  it('does NOT use a {project: legacyName} legacy fallback for AgentConversation', () => {
    // Locate the AgentConversation.filter call. We only need to
    // inspect the call itself (up to the closing paren), not the
    // surrounding Promise.allSettled block — other entities in the
    // same block still use the legacy fallback.
    const block = SOURCE.match(
      /base44\.entities\.AgentConversation\.filter\([^)]{0,200}\)/,
    );
    expect(block, 'Could not locate the AgentConversation.filter call').not.toBeNull();
    expect(block[0]).not.toMatch(/\{\s*project\s*:/);
  });

  it('uses {project_id: projectId} as the only AgentConversation filter', () => {
    // The fix replaces the mergeFbk fallback with a single
    // .filter({ project_id: projectId }, ...) call. Pin the exact
    // shape so a refactor can't quietly reintroduce the broken
    // fallback.
    expect(SOURCE).toMatch(
      /base44\.entities\.AgentConversation\.filter\(\s*\{\s*project_id:\s*projectId\s*\},\s*'-updated_date',\s*100\s*\)/,
    );
  });

  it('still uses the legacy fallback for KnowledgeBase (which has a project column)', () => {
    // Sanity check: the legacy fallback IS still correct for
    // entities that have the project column. Removing it from those
    // would be a regression. Pin the existing pattern.
    expect(SOURCE).toMatch(
      /base44\.entities\.KnowledgeBase\.filter\(\s*\{\s*project:\s*legacyName\s*\}/,
    );
    expect(SOURCE).toMatch(
      /base44\.entities\.AutomationTask\.filter\(\s*\{\s*project:\s*legacyName\s*\}/,
    );
  });
});
