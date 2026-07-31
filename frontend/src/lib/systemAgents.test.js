/**
 * Tests for the systemAgents helper — single source of truth for
 * "is this a platform-shipped agent that should be hidden from the
 * user-facing UI?"
 *
 * These tests guard the helper used in ChatInput, InvokePicker,
 * PlusMenu, MySpace, and AddAgentToProjectDialog. If the helper
 * regresses (e.g. someone drops the is_system field check), every
 * user-facing list would start showing general_assistant,
 * agent_builder, etc. — which is the exact bug the user reported.
 */

import { describe, it, expect } from 'vitest';
import { isSystemAgent, filterUserAgents, SYSTEM_AGENT_NAMES } from './systemAgents';

describe('isSystemAgent', () => {
  it('returns false for null / undefined', () => {
    expect(isSystemAgent(null)).toBe(false);
    expect(isSystemAgent(undefined)).toBe(false);
  });

  it('returns true when is_system=true on the record (backend source of truth)', () => {
    expect(isSystemAgent({ id: '1', name: 'General Assistant', is_system: true })).toBe(true);
  });

  it('returns true for legacy rows that pre-date the is_system column (name match)', () => {
    // No is_system field — falls back to the SYSTEM_AGENT_NAMES list.
    for (const name of SYSTEM_AGENT_NAMES) {
      expect(isSystemAgent({ id: '1', name })).toBe(true);
    }
  });

  it('returns false for user-created agents', () => {
    expect(isSystemAgent({ id: 'u1', name: 'My Custom Agent', is_system: false })).toBe(false);
    expect(isSystemAgent({ id: 'u2', name: 'Sales Assistant' })).toBe(false);
  });

  it('does NOT match a user agent whose name happens to contain a system keyword', () => {
    expect(isSystemAgent({ id: 'u3', name: 'general_assistant_clone' })).toBe(false);
    expect(isSystemAgent({ id: 'u4', name: 'My Power User Helper' })).toBe(false);
  });

  it('is robust to missing / weird fields', () => {
    expect(isSystemAgent({})).toBe(false);
    expect(isSystemAgent({ name: null })).toBe(false);
    expect(isSystemAgent({ name: 42 })).toBe(false);
    expect(isSystemAgent({ is_system: 'true' })).toBe(false); // must be boolean true
  });
});

describe('filterUserAgents', () => {
  it('drops system agents and keeps user agents', () => {
    const list = [
      { id: '1', name: 'general_assistant' },          // system
      { id: '2', name: 'My Agent' },                   // user
      { id: '3', name: 'agent_builder', is_system: true }, // system (explicit flag)
      { id: '4', name: 'Another User Agent' },         // user
    ];
    const filtered = filterUserAgents(list);
    expect(filtered.map((a) => a.id)).toEqual(['2', '4']);
  });

  it('does not mutate the input array', () => {
    const list = [{ id: '1', name: 'general_assistant' }, { id: '2', name: 'Mine' }];
    const copy = [...list];
    filterUserAgents(list);
    expect(list).toEqual(copy);
  });

  it('returns an empty array for non-array input', () => {
    expect(filterUserAgents(null)).toEqual([]);
    expect(filterUserAgents(undefined)).toEqual([]);
    expect(filterUserAgents('not an array')).toEqual([]);
  });

  it('returns an empty array when the input is empty', () => {
    expect(filterUserAgents([])).toEqual([]);
  });
});
