/**
 * Regression: useAgentBuilder must expose prefilledHint state and an
 * applySuggestion helper that pre-fills input + focuses, WITHOUT calling
 * startNewChat or handleSend. See spec
 * 2026-07-28-skill-agent-wait-for-input-design.md §3.2.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './useAgentBuilder.js'), 'utf8');

describe('useAgentBuilder.js — applySuggestion helper', () => {
  it('declares prefilledHint state', () => {
    expect(SOURCE).toMatch(/const\s*\[\s*prefilledHint\s*,\s*setPrefilledHint\s*\]\s*=\s*useState\(false\)/);
  });

  it('defines applySuggestion as a useCallback', () => {
    expect(SOURCE).toMatch(/const\s+applySuggestion\s*=\s*useCallback\s*\(\s*\(text\)\s*=>\s*\{/);
  });

  it('applySuggestion sets input, prefilledHint=true, and focuses inputRef', () => {
    const fn = SOURCE.match(/const\s+applySuggestion\s*=\s*useCallback\s*\(\s*\(text\)\s*=>\s*\{([\s\S]*?)\},\s*\[\]\s*\)/);
    expect(fn, 'applySuggestion body not found').not.toBeNull();
    expect(fn[1]).toMatch(/setInput\(text\)/);
    expect(fn[1]).toMatch(/setPrefilledHint\(true\)/);
    expect(fn[1]).toMatch(/inputRef\.current\?\.focus\(\)/);
  });

  it('applySuggestion does NOT call startNewChat or handleSend', () => {
    const fn = SOURCE.match(/const\s+applySuggestion\s*=\s*useCallback\s*\(\s*\(text\)\s*=>\s*\{([\s\S]*?)\},\s*\[\]\s*\)/);
    expect(fn, 'applySuggestion body not found').not.toBeNull();
    expect(fn[1]).not.toMatch(/startNewChat\(/);
    expect(fn[1]).not.toMatch(/handleSend\(/);
  });

  it('exposes prefilledHint, setPrefilledHint, applySuggestion in the return object', () => {
    expect(SOURCE).toMatch(/prefilledHint,/);
    expect(SOURCE).toMatch(/setPrefilledHint,/);
    expect(SOURCE).toMatch(/applySuggestion,/);
  });

  it('startNewChat still exists and still auto-sends (intentional path preserved)', () => {
    expect(SOURCE).toMatch(/const\s+startNewChat\s*=\s*useCallback/);
    expect(SOURCE).toMatch(/setTimeout\(\(\)\s*=>\s*handleSend\(promptText,\s*conv\),\s*100\)/);
  });
});
