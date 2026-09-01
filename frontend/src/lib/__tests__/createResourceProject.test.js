import { describe, it, expect } from 'vitest';
// pickCreateProject resolves the `project` string stamped onto a chat-created
// resource. Bug: Chat.jsx built the create payload as
//   { ...parsedFields, project: pendingProject || ungrouped }
// so `project:` came AFTER the spread and OVERRODE the LLM-parsed
// `fields.project` (which the create-dialog reliably fills from the
// "- Project：" prefill line). When no project chip was set in the chat
// (pendingProject empty), the task was stamped "Ungrouped" even though the
// user explicitly picked a project in the dialog.
import { pickCreateProject } from '../createResourceProject';

const UNGROUPED = 'Ungrouped';

describe('createResourceProject.pickCreateProject', () => {
  it('prefers the LLM-parsed project over the pending chat chip', () => {
    expect(pickCreateProject({ project: 'ACME' }, 'test2', UNGROUPED)).toBe('ACME');
  });

  it('uses parsed project when there is no pending chip (the reported bug)', () => {
    expect(pickCreateProject({ project: 'ACME' }, null, UNGROUPED)).toBe('ACME');
  });

  it('falls back to the pending chip when the LLM omitted project', () => {
    expect(pickCreateProject({}, 'ACME', UNGROUPED)).toBe('ACME');
    expect(pickCreateProject({ project: '' }, 'ACME', UNGROUPED)).toBe('ACME');
    expect(pickCreateProject({ project: '   ' }, 'ACME', UNGROUPED)).toBe('ACME');
  });

  it('falls back to Ungrouped when neither parsed nor chip is set', () => {
    expect(pickCreateProject({}, null, UNGROUPED)).toBe(UNGROUPED);
    expect(pickCreateProject({ project: '' }, '', UNGROUPED)).toBe(UNGROUPED);
  });

  it('treats parsed sentinel values (ungrouped/global/未分组) as "no project" and falls back', () => {
    expect(pickCreateProject({ project: 'Ungrouped' }, 'ACME', UNGROUPED)).toBe('ACME');
    expect(pickCreateProject({ project: 'global' }, 'ACME', UNGROUPED)).toBe('ACME');
    expect(pickCreateProject({ project: 'GLOBAL' }, 'ACME', UNGROUPED)).toBe('ACME');
    expect(pickCreateProject({ project: '未分组' }, 'ACME', UNGROUPED)).toBe('ACME');
    // ... and to Ungrouped when no chip either
    expect(pickCreateProject({ project: 'global' }, null, UNGROUPED)).toBe(UNGROUPED);
  });

  it('trims whitespace around a real parsed project', () => {
    expect(pickCreateProject({ project: '  ACME  ' }, null, UNGROUPED)).toBe('ACME');
  });
});
