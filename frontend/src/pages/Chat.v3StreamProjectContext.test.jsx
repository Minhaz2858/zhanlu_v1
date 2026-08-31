/**
 * Regression (2026-08-05): the v3 stream request body must carry
 * ``project_id`` and ``project_name`` whenever the chat has a project
 * context (URL has ``?project=`` / ``?projectName=`` OR pendingProject
 * is set). Without these, the backend's data-source runtime can't
 * extend the agent's bound KBs with the project's KBs — so an agent
 * running inside a project reports "no bound data sources"
 * even though the project clearly has a MySQL KB attached.
 *
 * Why source-text tests?
 * ----------------------
 * Same pattern as Chat.emptyPartials / Chat.projectContextLeak: no DOM,
 * no router, no mocks. The contract is in the JSX that builds the
 * stream request body.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx v3 stream request carries project context', () => {
  it('handleAgentSend passes project_id and project_name in the stream body', () => {
    // The handleAgentSend function builds the body for streamAgentResponse:
    //   {
    //     role: 'user',
    //     content: text,
    //     ...(file_urls ? { file_urls } : {}),
    //     ...(lang ? { lang } : {}),
    //     // ── new ──
    //     ...(projectId ? { project_id: projectId } : {}),
    //     ...(projectName ? { project_name: projectName } : {}),
    //   }
    // The project context is sourced from the current URL params
    // (read inside the function via window.location.search) so it
    // matches whatever navigation the user did to get here.
    //
    // Use a generous capture window — handleAgentSend is ~386 lines.
    // Anchor on the function declaration and the next `\n  }` at the
    // top level (a top-level function close inside the component is
    // exactly two-space-indented `  }`).
    const handleAgentSendMatch = SOURCE.match(
      /async\s+function\s+handleAgentSend\([\s\S]*?\)\s*\{([\s\S]*?)\n  \}\n/,
    );
    expect(handleAgentSendMatch, 'handleAgentSend function not found').not.toBeNull();
    const body = handleAgentSendMatch[1];
    // The streamAgentResponse body block must include project_id and
    // project_name fields. We assert the body object (passed as the
    // 2nd arg to streamAgentResponse) references both.
    expect(body).toMatch(/project_id/);
    expect(body).toMatch(/project_name/);
  });

  it('handleAgentSend sources project_id/project_name from window.location', () => {
    // The project context must come from the CURRENT URL params (the
    // navigation the user just did), not from sessionStorage or some
    // other sticky source — that's the whole reason this fix exists.
    const handleAgentSendMatch = SOURCE.match(
      /async\s+function\s+handleAgentSend\([\s\S]*?\)\s*\{([\s\S]*?)\n  \}\n/,
    );
    expect(handleAgentSendMatch).not.toBeNull();
    const body = handleAgentSendMatch[1];
    // Must reference window.location or URLSearchParams (the live URL
    // is the source of truth — not pendingProject state which can lag
    // behind a navigation).
    expect(body).toMatch(/window\.location|URLSearchParams/);
  });
});