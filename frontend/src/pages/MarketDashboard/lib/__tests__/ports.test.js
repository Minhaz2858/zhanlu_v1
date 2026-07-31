import { describe, it, expect } from 'vitest';
import { TYPO } from '../typography';
import { getChartTheme, rechartsTooltipStyle } from '../chartTheme';
import {
  getPriceChangeDirection,
  getPriceChangeTextClass,
  formatSignedPriceChange,
  getPriceChangeArrow,
  NO_DATA_LABEL,
} from '../priceChange';

describe('typography port', () => {
  it('exports TYPO with expected keys', () => {
    expect(TYPO.pageTitle).toContain('text-lg');
    expect(TYPO.priceData).toContain('font-mono');
  });
});

describe('chartTheme port', () => {
  it('returns dark theme tokens when isDark', () => {
    const t = getChartTheme(true);
    expect(t.grid).toBe('#2a2f3a');
    expect(t.tooltip.backgroundColor).toBe('#1c1f24');
  });
  it('returns light theme tokens when not isDark', () => {
    const t = getChartTheme(false);
    expect(t.grid).toBe('#e2e8f0');
  });
  it('rechartsTooltipStyle returns style object', () => {
    const t = getChartTheme(false);
    const s = rechartsTooltipStyle(t, false);
    expect(s).toHaveProperty('backgroundColor');
  });
});

describe('priceChange port', () => {
  it('classifies direction', () => {
    expect(getPriceChangeDirection(1)).toBe('increase');
    expect(getPriceChangeDirection(-1)).toBe('decrease');
    expect(getPriceChangeDirection(0)).toBe('unchanged');
    expect(getPriceChangeDirection(null)).toBe('no_data');
  });
  it('returns tailwind text class', () => {
    expect(getPriceChangeTextClass(1)).toContain('text-red');
    expect(getPriceChangeTextClass(-1)).toContain('text-green');
  });
  it('formats signed change', () => {
    expect(formatSignedPriceChange(1.5)).toBe('+1.5');
    expect(formatSignedPriceChange(0)).toBe('0.00');
    expect(formatSignedPriceChange(null)).toBe(NO_DATA_LABEL);
  });
  it('arrow mapping', () => {
    expect(getPriceChangeArrow(1)).toBe('up');
    expect(getPriceChangeArrow(-1)).toBe('down');
    expect(getPriceChangeArrow(0)).toBe('flat');
  });
});
