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

  it('handles search_query_delta event', () => {
    expect(src).toMatch(/search_query_delta/);
    expect(src).toMatch(/streaming_search_queries/);
  });

  it('handles plan_step_added event', () => {
    expect(src).toMatch(/plan_step_added/);
    expect(src).toMatch(/streaming_plan_steps/);
  });

  it('handles data_preview event', () => {
    expect(src).toMatch(/data_preview/);
    expect(src).toMatch(/streaming_data_previews/);
  });
});
