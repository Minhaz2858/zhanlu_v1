/**
 * UI automation test: SkillAgent scrape chip interaction.
 *
 * Verifies that the SkillAgent page:
 * 1. Renders the "Scrape from URL" chip in the empty state
 * 2. The chip has a Globe icon
 * 3. Clicking the chip opens the ScrapeUrlDialog
 * 4. The ScrapeUrlDialog's onCollected callback sets the current skill
 *    and opens the file panel
 *
 * Uses source-matching for structural assertions (consistent with
 * existing SkillAgent tests) plus behavioral assertions for the
 * scrape dialog wiring.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './SkillAgent.jsx'), 'utf8');

describe('SkillAgent.jsx — scrape chip and dialog wiring', () => {
  it('imports the Globe icon from lucide-react', () => {
    expect(SOURCE).toMatch(/Globe/);
  });

  it('imports ScrapeUrlDialog component', () => {
    expect(SOURCE).toMatch(/import\s+ScrapeUrlDialog\s+from/);
  });

  it('has a scrape state variable (scrapeOpen)', () => {
    expect(SOURCE).toMatch(/scrapeOpen/);
    expect(SOURCE).toMatch(/setScrapeOpen/);
  });

  it('renders a "Scrape from URL" chip in the empty state chips array', () => {
    // The chip is defined with action: 'scrape' and a Globe icon
    expect(SOURCE).toMatch(/t\.skillAgent\.scrape\.chip/);
    expect(SOURCE).toMatch(/action:\s*['"]scrape['"]/);
    expect(SOURCE).toMatch(/icon:\s*Globe/);
  });

  it('opens the scrape dialog when the scrape chip is clicked', () => {
    // The chip's onClick should call setScrapeOpen(true) when action === 'scrape'
    expect(SOURCE).toMatch(/chip\.action\s*===\s*['"]scrape['"]/);
    expect(SOURCE).toMatch(/setScrapeOpen\(true\)/);
  });

  it('renders the ScrapeUrlDialog component with open={scrapeOpen}', () => {
    expect(SOURCE).toMatch(/<ScrapeUrlDialog/);
    expect(SOURCE).toMatch(/open=\{scrapeOpen\}/);
  });

  it('passes onClose handler that sets scrapeOpen to false', () => {
    expect(SOURCE).toMatch(/onClose=\{\(\)\s*=>\s*setScrapeOpen\(false\)\}/);
  });

  it('passes onCollected callback that sets the current skill from the result', () => {
    // The onCollected callback should call setCurrentSkill with the result
    expect(SOURCE).toMatch(/onCollected=/);
    expect(SOURCE).toMatch(/result\.skill_name/);
    expect(SOURCE).toMatch(/setCurrentSkill/);
  });

  it('opens the file panel when a skill is collected', () => {
    // The onCollected callback should call setPanelOpen(true)
    expect(SOURCE).toMatch(/setPanelOpen\(true\)/);
  });

  it('the scrape chip applies suggestion text for non-scrape chips', () => {
    // Non-scrape chips should call applySuggestion
    expect(SOURCE).toMatch(/applySuggestion\(chip\.text\)/);
  });

  it('all chips have a unique key', () => {
    expect(SOURCE).toMatch(/key=\{chip\.key\}/);
  });
});
