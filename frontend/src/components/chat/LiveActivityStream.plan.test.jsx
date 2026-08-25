/** 2026-08-25: live-streaming spec — verify live plan section is rendered. */
import fs from 'fs';
import path from 'path';

describe('LiveActivityStream plan section', () => {
  const lasPath = path.join(__dirname, 'LiveActivityStream.jsx');
  let src;
  beforeAll(() => {
    src = fs.readFileSync(lasPath, 'utf-8');
  });

  it('renders plan steps with step count', () => {
    // Should iterate over streamingPlanSteps
    expect(src).toMatch(/streamingPlanSteps\.map/);
    expect(src).toMatch(/streamingPlanSteps\.length/);
  });

  it('uses blue color for plan section', () => {
    expect(src).toMatch(/border-blue-500\/30/);
    expect(src).toMatch(/bg-blue-500\/5/);
  });

  it('shows spinning loader on last step when streaming', () => {
    // animate-spin on the last step
    const hasSpinner = /streamingPlanSteps\.length\s*-\s*1[\s\S]{0,200}animate-spin/;
    expect(src).toMatch(hasSpinner);
  });

  it('uses ClipboardList icon', () => {
    expect(src).toMatch(/ClipboardList/);
  });

  it('has its own collapse state', () => {
    expect(src).toMatch(/const \[planOpen/);
  });
});
