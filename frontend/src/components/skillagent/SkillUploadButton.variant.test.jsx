/**
 * Regression: SkillUploadButton must support a variant="cta" prop for
 * the empty-state drop zone (larger dashed-border button). The compact
 * variant (default) is unchanged. See spec
 * 2026-07-28-skill-agent-wait-for-input-design.md §3.5.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './SkillUploadButton.jsx'), 'utf8');

describe('SkillUploadButton.jsx — variant="cta" support', () => {
  it('accepts variant, ctaLabel, ctaHint props', () => {
    expect(SOURCE).toMatch(/export\s+default\s+function\s+SkillUploadButton\s*\(\s*\{\s*onUploaded\s*,\s*disabled\s*,\s*variant\s*=\s*['"]compact['"]\s*,\s*ctaLabel\s*,\s*ctaHint\s*\}\s*\)/);
  });

  it('defaults variant to "compact"', () => {
    expect(SOURCE).toMatch(/variant\s*=\s*['"]compact['"]/);
  });

  it('renders a CTA-styled trigger when variant === "cta"', () => {
    expect(SOURCE).toMatch(/variant\s*===\s*['"]cta['"]/);
    expect(SOURCE).toMatch(/\{ctaLabel\}/);
    expect(SOURCE).toMatch(/\{ctaHint\}/);
  });
});
