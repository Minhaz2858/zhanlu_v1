import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { watchForDashboardEdits } from './dashboardStreamWatcher';

// `onRefresh` is intentionally deferred via setTimeout(..., 0) so the panel can
// mount + DashboardViewer's forwardRef can attach before the refresh fires.
// Tests use fake timers so we can flush the defer deterministically.
describe('watchForDashboardEdits', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns seenCount when tool_calls is empty', () => {
    const onOpen = vi.fn();
    const onRefresh = vi.fn();
    expect(watchForDashboardEdits([], 0, { onOpen, onRefresh })).toBe(0);
    expect(watchForDashboardEdits(undefined, 5, { onOpen, onRefresh })).toBe(5);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('ignores non-dashboard tool calls', () => {
    const onOpen = vi.fn();
    const onRefresh = vi.fn();
    const toolCalls = [
      { name: 'ask_data_agent', results: { rows: [1] } },
      { name: 'web_search', results: { hits: [] } },
    ];
    expect(watchForDashboardEdits(toolCalls, 0, { onOpen, onRefresh })).toBe(0);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('triggers onOpen + onRefresh on first create_dashboard result', () => {
    const onOpen = vi.fn();
    const onRefresh = vi.fn();
    const toolCalls = [{ name: 'create_dashboard', results: { id: 'd1' } }];
    expect(watchForDashboardEdits(toolCalls, 0, { onOpen, onRefresh })).toBe(1);
    expect(onOpen).toHaveBeenCalledTimes(1);
    vi.runAllTimers();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('dedupes repeated invocations within the same message', () => {
    const onOpen = vi.fn();
    const onRefresh = vi.fn();
    const toolCalls = [{ name: 'update_dashboard', results: { id: 'd1' } }];
    expect(watchForDashboardEdits(toolCalls, 0, { onOpen, onRefresh })).toBe(1);
    expect(watchForDashboardEdits(toolCalls, 1, { onOpen, onRefresh })).toBe(1);
    expect(onOpen).toHaveBeenCalledTimes(1);
    vi.runAllTimers();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('fires again when a new dashboard tool call appears later in the message', () => {
    const onOpen = vi.fn();
    const onRefresh = vi.fn();
    const partial = [{ name: 'update_dashboard', results: { id: 'd1' } }];
    const full = [
      ...partial,
      { name: 'undo_dashboard_edit', results: { id: 'd1' } },
    ];
    expect(watchForDashboardEdits(partial, 0, { onOpen, onRefresh })).toBe(1);
    expect(watchForDashboardEdits(full, 1, { onOpen, onRefresh })).toBe(2);
    expect(onOpen).toHaveBeenCalledTimes(2);
    vi.runAllTimers();
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it('treats in-flight tool_progress (no result) as not yet completed', () => {
    const onOpen = vi.fn();
    const onRefresh = vi.fn();
    const inFlight = [{ name: 'create_dashboard', args: { name: 'x' } }];
    expect(watchForDashboardEdits(inFlight, 0, { onOpen, onRefresh })).toBe(0);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('accepts tool_name as a fallback for the tool identifier', () => {
    const onOpen = vi.fn();
    const onRefresh = vi.fn();
    const toolCalls = [{ tool_name: 'update_dashboard', results: { id: 'd1' } }];
    expect(watchForDashboardEdits(toolCalls, 0, { onOpen, onRefresh })).toBe(1);
    expect(onOpen).toHaveBeenCalledTimes(1);
    vi.runAllTimers();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});
