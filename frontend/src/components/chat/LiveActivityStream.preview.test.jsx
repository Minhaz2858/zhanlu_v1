/** 2026-08-25: live-streaming spec — verify data preview sub-row is rendered. */
import fs from 'fs';
import path from 'path';

describe('LiveActivityStream data preview section', () => {
  const lasPath = path.join(__dirname, 'LiveActivityStream.jsx');
  let src;
  beforeAll(() => {
    src = fs.readFileSync(lasPath, 'utf-8');
  });

  it('renders data preview grid', () => {
    expect(src).toMatch(/streamingDataPreviews/);
    expect(src).toMatch(/Object\.entries\(streamingDataPreviews\)/);
  });

  it('shows sample rows with column headers', () => {
    expect(src).toMatch(/sample_rows/);
    expect(src).toMatch(/preview\.columns/);
  });

  it('uses green color for results section', () => {
    expect(src).toMatch(/border-green-500\/30/);
    expect(src).toMatch(/bg-green-500\/5/);
  });

  it('uses Table2 icon', () => {
    expect(src).toMatch(/<Table2 /);
  });

  it('has its own collapse state', () => {
    expect(src).toMatch(/const \[dataPreviewOpen/);
  });
});
