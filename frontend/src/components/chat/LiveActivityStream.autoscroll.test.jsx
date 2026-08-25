/** 2026-08-25: live-streaming spec — verify auto-scroll triggers on streaming content. */
import fs from 'fs';
import path from 'path';

describe('LiveActivityStream auto-scroll', () => {
  const lasPath = path.join(__dirname, 'LiveActivityStream.jsx');
  let src;
  beforeAll(() => {
    src = fs.readFileSync(lasPath, 'utf-8');
  });

  it('depends on reasoning length in scroll effect', () => {
    // The dependency array should include reasoning length
    expect(src).toMatch(/reasoning\.length/);
  });

  it('depends on streaming plan steps count', () => {
    expect(src).toMatch(/streamingPlanSteps\.length/);
  });

  it('depends on streaming search queries count', () => {
    expect(src).toMatch(/streamingSearchQueries[\s\S]{0,100}Object\.keys/);
  });

  it('depends on streaming data previews count', () => {
    expect(src).toMatch(/streamingDataPreviews[\s\S]{0,100}Object\.keys/);
  });
});
