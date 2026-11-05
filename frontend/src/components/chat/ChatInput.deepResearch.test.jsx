/**
 * Contract tests for the Deep Research composer controls (v1).
 *
 * Pins the frontend contract so the feature can't silently regress:
 *   - ChatInput accepts `researchConfig` + `onResearchChange` props.
 *   - When `researchConfig` is enabled it renders depth chips
 *     (Quick/Balanced/Deep) and scope chips (Web/Internal/Both).
 *   - Sending includes the research config in the message payload
 *     (backend gates it behind DEEP_RESEARCH_ENABLED).
 *
 * Same source-text pattern as ChatInput.inheritedBadge.test.jsx.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ChatInput.jsx'), 'utf8');

describe('ChatInput.jsx deep research controls', () => {
  it('declares researchConfig + onResearchChange as props', () => {
    // Window widened (P1 department props lengthened the destructuring).
    const propList = SOURCE.match(/function\s+ChatInput\s*\(\s*\{[\s\S]{0,1400}?\}\s*\)/);
    expect(propList, 'ChatInput props destructure not found').not.toBeNull();
    expect(propList[0]).toMatch(/\bresearchConfig\b/);
    expect(propList[0]).toMatch(/\bonResearchChange\b/);
  });

  it('renders the three depth chips when research is enabled', () => {
    // Gate on researchConfig truthiness + render "quick/balanced/deep".
    expect(SOURCE).toMatch(/researchConfig\s*&&/);
    expect(SOURCE).toMatch(/'quick'/);
    expect(SOURCE).toMatch(/'balanced'/);
    expect(SOURCE).toMatch(/'deep'/);
  });

  it('renders the three scope chips when research is enabled', () => {
    expect(SOURCE).toMatch(/'web'/);
    expect(SOURCE).toMatch(/'internal'/);
    expect(SOURCE).toMatch(/'both'/);
  });

  it('includes research config in the send payload', () => {
    // handleSend must forward the research config to onSend as opts.
    expect(SOURCE).toMatch(/onSend\(\s*value\.trim\(\),\s*\{\s*research:/);
  });
});
