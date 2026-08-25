/** 2026-08-25: live-streaming spec — verify reasoning section has char count + cursor. */
import fs from 'fs';
import path from 'path';

describe('LiveActivityStream reasoning section', () => {
  const lasPath = path.join(__dirname, 'LiveActivityStream.jsx');
  let src;
  beforeAll(() => {
    src = fs.readFileSync(lasPath, 'utf-8');
  });

  it('renders char count for reasoning', () => {
    // The reasoning section should show the character count
    expect(src).toMatch(/reasoning\.length\.toLocaleString\(\)/);
  });

  it('shows pulsing cursor when streaming', () => {
    // The pre block should have an animate-pulse cursor when isStreaming
    const hasCursor = /isStreaming[\s\S]{0,200}animate-pulse[\s\S]{0,200}▍/;
    expect(src).toMatch(hasCursor);
  });

  it('uses purple color for thinking section', () => {
    expect(src).toMatch(/border-purple-500\/30/);
    expect(src).toMatch(/bg-purple-500\/5/);
  });
});
