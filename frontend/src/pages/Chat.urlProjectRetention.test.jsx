/**
 * Regression (2026-08-05): the mount-time URL cleanup in Chat.jsx
 * was deleting ``?project=``, ``?projectName=`` and ``?agentName=``
 * from the URL — leaving it as ``?conv=...`` only. As a result:
 *   - Reloads lost the project context (the conv rehydration never
 *     re-wrote the URL).
 *   - ``handleAgentSend`` (which reads the v3 stream body params
 *     from window.location) had no project_id / project_name to
 *     forward to the backend.
 *   - The backend's ``prepare_data_source_runtime`` then had no
 *     project context to extend the agent's bound KBs with — even
 *     when the AgentConversation row itself was tagged with
 *     ``project_id``, the user's URL said the chat was in
 *     "Ungrouped" / default.
 *   - The ``load_memory_snapshot`` also lacked project context, so
 *     per-project memory scope was bypassed on every reload.
 *
 * Fix: only the truly transient one-shot params (``?agent=``,
 * ``?prefill=``, ``?autorun=``) are stripped on mount. The conv
 * rehydration block also writes the conv's project_id / project_name
 * back into the URL on every reload, so handleAgentSend and the
 * data-source runtime see the right project context even for convs
 * that originally had no project params.
 *
 * Note (2026-08-06): the conv rehydration does NOT write agentName
 * back into the URL. The agent context is bound via setActiveAgent
 * (React state) and consumed by handleAgentSend from there — no code
 * reads agentName from the URL for the v3 stream body. Writing it
 * to the URL on reload changed the visible URL bar with no
 * functional benefit, so the URL stays minimal. ``?agentName=`` is
 * only set on user-initiated navigations (sidebar session select,
 * ProjectDetail recent-chats click).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx URL keeps project context on mount', () => {
  it('mount-time URL cleanup does NOT delete ?project= or ?projectName=', () => {
    // Find the forEach block that strips transient params. The
    // previous version deleted ``['agent', 'prefill', 'project',
    // 'projectName', 'agentName']`` — that's the bug. The fix
    // narrows the list to ``['agent', 'prefill', 'autorun']`` so
    // the project / agentName context stays in the URL across
    // reloads (handleAgentSend reads them from window.location to
    // build the v3 stream body).
    const cleanupMatch = SOURCE.match(
      /\[([\s\S]{0,200}?)\]\.forEach\(\(k\)\s*=>\s*\{[\s\S]{0,400}?url\.searchParams\.delete\(k\)/,
    );
    expect(cleanupMatch, 'cleanup forEach not found').not.toBeNull();
    const listSrc = cleanupMatch[1];
    const params = [...listSrc.matchAll(/'([a-zA-Z]+)'/g)].map((m) => m[1]);
    // Must NOT include the project context params.
    expect(params).not.toContain('project');
    expect(params).not.toContain('projectName');
    expect(params).not.toContain('agentName');
    // Must still strip the truly transient one-shot params.
    expect(params).toContain('agent');
    expect(params).toContain('prefill');
  });

  it('conv rehydration writes the conv project_id / project_name into the URL', () => {
    // After the conv is loaded, the code must push the conv's
    // project context back into the URL so handleAgentSend sees it.
    // Anchor on the URL sync block (which writes conv.project_id
    // and conv.project_name into window.location) rather than on
    // the surrounding .then() — there are too many nested
    // ``}).catch(`` markers to make a regex match reliable.
    const projectIdSync = SOURCE.match(
      /url\.searchParams\.set\(['"]project['"]\s*,\s*conv\.project_id[\s\S]{0,1500}?window\.history\.replaceState/,
    );
    expect(projectIdSync, 'URL sync block (project_id → window.history.replaceState) not found').not.toBeNull();
    const body = projectIdSync[0];
    // Must set projectName from conv.project_name (or conv.project).
    expect(body).toMatch(/url\.searchParams\.set\(['"]projectName['"]\s*,\s*(conv\.project_name|conv\.project|pname)/);
    // Must NOT set agentName — the agent context is bound via
    // setActiveAgent (React state) and consumed from there, not
    // from the URL. Writing it on reload would mutate the visible
    // URL bar with no functional benefit.
    expect(
      body,
      'conv rehydration must NOT write agentName into the URL (agent is bound via setActiveAgent React state)',
    ).not.toMatch(/url\.searchParams\.set\(['"]agentName['"]\s*,\s*conv\.agent_name/);
    // Must seed the input's pendingProject state so the project
    // chip in the chat input shows the right project on reload.
    expect(body).toMatch(/setPendingProjectState/);
    expect(body).toMatch(/setPendingProjectId/);
  });
});
