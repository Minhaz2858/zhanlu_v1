/** 2026-08-25: live-streaming spec — verify Chat.jsx handles 4 new SSE event types. */
import fs from 'fs';
import path from 'path';

describe('Chat.jsx streaming state', () => {
  const chatPath = path.join(__dirname, 'Chat.jsx');
  let src;
  beforeAll(() => {
    src = fs.readFileSync(chatPath, 'utf-8');
  });

  it('handles reasoning_delta event', () => {
    expect(src).toMatch(/reasoning_delta/);
  });

  it('does not handle search_query_delta (removed 2026-08-25)', () => {
    // The live-streaming Search section was removed per user request; the
    // backend still emits the event but Chat.jsx must NOT store it on the
    // message. This pins the deliberate non-handling.
    expect(src).not.toMatch(/streaming_search_queries/);
  });

  it('handles plan_step_added event', () => {
    expect(src).toMatch(/plan_step_added/);
    expect(src).toMatch(/streaming_plan_steps/);
  });

  it('handles plan_step_completed event', () => {
    // 2026-08-27: the backend ticks plan steps off by tool evidence; the
    // frontend must mark the matching step done in the live checklist.
    expect(src).toMatch(/plan_step_completed/);
    expect(src).toMatch(/status: 'done'/);
  });

  it('does not handle data_preview (removed 2026-08-25)', () => {
    // Same deliberate removal as search_query_delta — inline data previews
    // are now attached to tool rows via typed live_event `data_offer`.
    expect(src).not.toMatch(/streaming_data_previews/);
  });
});
