/**
 * Unit tests for the parallel-session tab helpers
 * (frontend/src/lib/sessionTabs.js).
 */
import { describe, it, expect } from 'vitest';
import { openTab, closeTab } from './sessionTabs';

const MAX = 8;

describe('openTab', () => {
  it('adds a tab when the session is not open', () => {
    const tabs = openTab([], 'a', 'Chat A', MAX);
    expect(tabs).toEqual([{ sessionId: 'a', title: 'Chat A' }]);
  });

  it('dedupes an already-open session and updates its title', () => {
    const tabs = openTab([{ sessionId: 'a', title: 'Old' }], 'a', 'New Title', MAX);
    expect(tabs).toEqual([{ sessionId: 'a', title: 'New Title' }]);
  });

  it('caps the number of tabs at max by dropping the oldest', () => {
    let tabs = [];
    for (let i = 0; i < MAX; i += 1) tabs = openTab(tabs, `s${i}`, `Chat ${i}`, MAX);
    expect(tabs.length).toBe(MAX);
    tabs = openTab(tabs, 'overflow', 'Overflow', MAX);
    expect(tabs.length).toBe(MAX);
    expect(tabs.some((t) => t.sessionId === 'overflow')).toBe(true);
    expect(tabs.some((t) => t.sessionId === 's0')).toBe(false);
  });

  it('returns a new array (immutable)', () => {
    const prev = [];
    const next = openTab(prev, 'a', 'A', MAX);
    expect(next).not.toBe(prev);
  });
});

describe('closeTab', () => {
  it('removes the tab and falls back to the next neighbor (browser standard)', () => {
    const tabs = [
      { sessionId: 'a', title: 'A' },
      { sessionId: 'b', title: 'B' },
      { sessionId: 'c', title: 'C' },
    ];
    const { tabs: next, fallbackId } = closeTab(tabs, 'b');
    expect(next.map((t) => t.sessionId)).toEqual(['a', 'c']);
    expect(fallbackId).toBe('c');
  });

  it('falls back to the previous tab when closing the last one', () => {
    const tabs = [
      { sessionId: 'a', title: 'A' },
      { sessionId: 'b', title: 'B' },
    ];
    const { tabs: next, fallbackId } = closeTab(tabs, 'b');
    expect(next.map((t) => t.sessionId)).toEqual(['a']);
    expect(fallbackId).toBe('a');
  });

  it('falls back to the next tab when closing the first', () => {
    const tabs = [
      { sessionId: 'a', title: 'A' },
      { sessionId: 'b', title: 'B' },
    ];
    const { tabs: next, fallbackId } = closeTab(tabs, 'a');
    expect(next.map((t) => t.sessionId)).toEqual(['b']);
    expect(fallbackId).toBe('b');
  });

  it('returns null fallback when closing the last tab', () => {
    const { tabs: next, fallbackId } = closeTab([{ sessionId: 'a', title: 'A' }], 'a');
    expect(next).toEqual([]);
    expect(fallbackId).toBe(null);
  });

  it('ignores unknown session ids', () => {
    const tabs = [{ sessionId: 'a', title: 'A' }];
    const { tabs: next, fallbackId } = closeTab(tabs, 'zzz');
    expect(next).toEqual(tabs);
    expect(fallbackId).toBe(null);
  });
});
