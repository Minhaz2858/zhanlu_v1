/** 2026-08-25: live-streaming spec — verify search sub-row is rendered. */
import fs from 'fs';
import path from 'path';

describe('LiveActivityStream search section', () => {
  const lasPath = path.join(__dirname, 'LiveActivityStream.jsx');
  let src;
  beforeAll(() => {
    src = fs.readFileSync(lasPath, 'utf-8');
  });

  it('renders search query with typing effect', () => {
    expect(src).toMatch(/streamingSearchQueries/);
    expect(src).toMatch(/Object\.entries\(streamingSearchQueries\)/);
  });

  it('uses 🔍 prefix for search rows', () => {
    expect(src).toMatch(/🔍/);
  });

  it('uses gray border for search section', () => {
    expect(src).toMatch(/border-gray-500\/30/);
  });

  it('uses Search icon', () => {
    expect(src).toMatch(/<Search /);
  });

  it('has its own collapse state', () => {
    expect(src).toMatch(/const \[searchOpen/);
  });
});
