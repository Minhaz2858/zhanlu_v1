/**
 * Regression (2026-08-05): sidebar → selectSession used to only
 * write ``?conv=...`` to the URL — never ``?project=`` or
 * ``?projectName=``. As a result, clicking a conv in a different
 * sidebar project (e.g. switching between a project and Global) left the URL as
 * ``?conv=...`` only, so the v3 stream request body (read from
 * window.location by Chat.jsx's handleAgentSend) had no
 * project_id to scope the data-source runtime with. The agent
 * then fell back to per-user memory across all projects and
 * every conv in every project recalled the same
 * "Q2 2026 sales report" content. Same fix on ``newChat`` so
 * "+ New Chat" in a sidebar group also forwards the project
 * context.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ChatSessionContext.jsx'), 'utf8');

describe('ChatSessionContext.jsx selectSession / newChat URL sync', () => {
  it('selectSession writes ?project= and ?projectName= alongside ?conv=', () => {
    // Locate the selectSession useCallback body. The URL sync block
    // lives inside it (right after ``setPendingProjectState(...)``
    // and ``setPendingProjectId(...)``).
    const selectSessionMatch = SOURCE.match(
      /const\s+selectSession\s*=\s*useCallback\([\s\S]*?\},[\s\S]*?\);/,
    );
    expect(selectSessionMatch, 'selectSession useCallback not found').not.toBeNull();
    const body = selectSessionMatch[0];
    // The URL sync block must set/delete the project, projectName,
    // AND agentName params — same shape as ProjectDetail.jsx
    // onOpenConv. The previous version only set/deleted ?conv=.
    expect(body).toMatch(/searchParams\.set\(['"]project['"]/);
    expect(body).toMatch(/searchParams\.set\(['"]projectName['"]/);
    expect(body).toMatch(/searchParams\.set\(['"]agentName['"]/);
    // (2026-08-31) The null branch must only clear project params for
    // BRAND-NEW sessions (no conversation_id yet) — a conv-linked
    // session whose row lacks the FK (legacy backfill) must PRESERVE
    // the URL's existing project context, otherwise a reload loses
    // ?project= / ?projectName= (the user-reported bug). A brand-new
    // session still clears them so a fresh global chat never inherits
    // a stale project.
    expect(body).toMatch(/!s\.conversation_id/);
    expect(body).toMatch(/searchParams\.delete\(['"]project['"]/);
    expect(body).toMatch(/searchParams\.delete\(['"]projectName['"]/);
  });

  it('newChat writes ?projectName= (drops ?conv= and ?agentName=)', () => {
    // The newChat useCallback must update the URL too — it cleared
    // activeId and set pendingProject, but before this fix it
    // never touched the URL, so a "+ New Chat" in a sidebar
    // group (e.g. Marketing Team) still left the URL as
    // ``?conv=<previous>`` with no projectName.
    //
    // We deliberately do NOT set ``?project=`` here — the newChat
    // caller (sidebar "+ New Chat" button) only has the project
    // NAME, not the FK. The chat's own URL-driven resolution will
    // look up the FK by name on the server. So the test asserts
    // the FK param is explicitly deleted, not set.
    const newChatMatch = SOURCE.match(
      /const\s+newChat\s*=\s*useCallback\([\s\S]*?\},[^;]*\);/,
    );
    expect(newChatMatch, 'newChat useCallback not found').not.toBeNull();
    const body = newChatMatch[0];
    expect(body).toMatch(/searchParams\.set\(['"]projectName['"]/);
    // Must drop ?conv= and ?agentName= so the next send creates a
    // fresh conv instead of reusing the previous one.
    expect(body).toMatch(/searchParams\.delete\(['"]conv['"]/);
    expect(body).toMatch(/searchParams\.delete\(['"]agentName['"]/);
    // Must drop ?project= too — the FK is unknown on the client
    // for sidebar-initiated new chats, so don't leak a stale one.
    expect(body).toMatch(/searchParams\.delete\(['"]project['"]/);
  });
});
