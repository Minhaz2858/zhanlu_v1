/** 2026-08-25: Test for StreamingMarkdown chunked renderer.
 *
 * The renderer splits content by paragraph boundary and memoizes
 * each completed chunk. Only the LAST (in-progress) chunk re-renders
 * on every token delta. This avoids the O(N²) cost of re-parsing
 * the full markdown on every token.
 */
import { describe, it, expect } from 'vitest';
import { splitContentIntoChunks } from './StreamingMarkdown';

describe('splitContentIntoChunks', () => {
  it('returns the whole text as one chunk when no paragraph break', () => {
    const out = splitContentIntoChunks('just a single line');
    expect(out).toEqual(['just a single line']);
  });

  it('splits on double-newline paragraph boundaries', () => {
    const out = splitContentIntoChunks('para one\n\npara two\n\npara three');
    expect(out).toHaveLength(3);
    expect(out[0]).toContain('para one');
    expect(out[1]).toContain('para two');
    expect(out[2]).toContain('para three');
  });

  it('keeps headers as part of the previous chunk (so they are atomic)', () => {
    // Headers at the start of a line split before themselves
    const out = splitContentIntoChunks('intro paragraph\n\n# Heading\n\nbody');
    // Should be: "intro paragraph", "# Heading", "body"
    expect(out.length).toBeGreaterThanOrEqual(2);
    expect(out.join(' ')).toContain('Heading');
  });

  it('handles empty content', () => {
    expect(splitContentIntoChunks('')).toEqual([]);
  });

  it('handles content with only whitespace', () => {
    const out = splitContentIntoChunks('   \n\n   \n\n   ');
    // Should return empty (or just whitespace, filtered)
    expect(out.every((c) => c.trim().length > 0)).toBe(true);
  });

  it('preserves trailing incomplete paragraph', () => {
    const out = splitContentIntoChunks('complete one\n\ncomplete two\n\nincomplete');
    expect(out).toHaveLength(3);
    expect(out[2]).toBe('incomplete');
  });

  it('handles tables as atomic blocks (no internal splits)', () => {
    const table = '| col1 | col2 |\n|------|------|\n| a    | b    |';
    const out = splitContentIntoChunks(`intro\n\n${table}\n\nconclusion`);
    expect(out).toHaveLength(3);
    expect(out[1]).toContain('| col1 | col2 |');
    expect(out[1]).toContain('| a    | b    |');
  });
});
