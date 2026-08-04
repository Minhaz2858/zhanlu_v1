import { describe, it, expect } from 'vitest';
import {
  niceTicks,
  computeYAxis,
  normalizeForecastPoints,
} from '../forecastChartMath';

describe('niceTicks', () => {
  it('brackets the data with evenly spaced round-step ticks', () => {
    const { domain, ticks } = niceTicks(6010, 6385, 6);
    expect(domain[0]).toBeLessThanOrEqual(6010);
    expect(domain[1]).toBeGreaterThanOrEqual(6385);
    expect(ticks[0]).toBe(domain[0]);
    expect(ticks[ticks.length - 1]).toBe(domain[1]);
    // even spacing
    const step = ticks[1] - ticks[0];
    for (let i = 1; i < ticks.length; i++) {
      expect(ticks[i] - ticks[i - 1]).toBeCloseTo(step, 6);
    }
    // round step (1/2/5 × 10^n)
    const mag = 10 ** Math.floor(Math.log10(step));
    expect([1, 2, 5, 10]).toContain(step / mag);
  });

  it('handles equal lo/hi without collapsing', () => {
    const { domain, ticks } = niceTicks(6200, 6200, 6);
    expect(domain[0]).toBeLessThan(6200);
    expect(domain[1]).toBeGreaterThan(6200);
    expect(ticks.length).toBeGreaterThanOrEqual(2);
  });

  it('handles swapped lo/hi', () => {
    const { domain } = niceTicks(6385, 6010, 6);
    expect(domain[0]).toBeLessThanOrEqual(6010);
    expect(domain[1]).toBeGreaterThanOrEqual(6385);
  });
});

describe('computeYAxis', () => {
  it('ignores null/0/negative sentinels so the domain hugs real prices', () => {
    const points = [
      { actual: 6100, bear: 0, prev_ai_low: null },
      { actual: 6250, bull: 6400 },
      { actual: 6180, bear: -5 },
    ];
    const { domain, ticks } = computeYAxis(points);
    expect(domain[0]).toBeGreaterThan(0);
    expect(domain[0]).toBeLessThanOrEqual(6100);
    expect(domain[1]).toBeGreaterThanOrEqual(6400);
    expect(ticks.length).toBeGreaterThanOrEqual(2);
  });

  it('falls back to auto domain when no usable values exist', () => {
    expect(computeYAxis([])).toEqual({ domain: ['auto', 'auto'], ticks: undefined });
    expect(computeYAxis([{ actual: null, bear: 0 }])).toEqual({
      domain: ['auto', 'auto'],
      ticks: undefined,
    });
  });
});

describe('normalizeForecastPoints', () => {
  it('anchors the forecast fan at the last actual when the backend emits no today point', () => {
    const raw = [
      { date: '2026-07-28', zone: 'past', actual: 6100 },
      { date: '2026-07-29', zone: 'past', actual: 6125 },
      // gap: backend skipped 07-30..08-03 (no actuals, no today point)
      { date: '2026-08-04', zone: 'forecast', base: 6130, bull: 6200, bear: 6050 },
      { date: '2026-08-05', zone: 'forecast', base: 6140, bull: 6220, bear: 6040 },
    ];
    const { points, hasForecast } = normalizeForecastPoints(raw);
    expect(hasForecast).toBe(true);
    const anchor = points.find((p) => p.zone === 'today');
    expect(anchor).toBeDefined();
    expect(anchor.date).toBe('2026-07-29');
    expect(anchor.base).toBe(6125);
    expect(anchor.bull).toBe(6125);
    expect(anchor.bear).toBe(6125);
    // anchor sits directly before the first forecast point — no gap
    const anchorIdx = points.indexOf(anchor);
    expect(points[anchorIdx + 1].zone).toBe('forecast');
  });

  it('leaves a backend-emitted today point untouched', () => {
    const raw = [
      { date: '2026-08-02', zone: 'past', actual: 6100 },
      { date: '2026-08-03', zone: 'today', actual: 6120, base: 6120, bull: 6120, bear: 6120 },
      { date: '2026-08-04', zone: 'forecast', base: 6130, bull: 6200, bear: 6050 },
    ];
    const { points, hasForecast } = normalizeForecastPoints(raw);
    expect(hasForecast).toBe(true);
    const todays = points.filter((p) => p.zone === 'today');
    expect(todays).toHaveLength(1);
    expect(todays[0].date).toBe('2026-08-03');
    expect(todays[0].base).toBe(6120);
  });

  it('returns past-only points unchanged with hasForecast=false', () => {
    const raw = [
      { date: '2026-07-28', zone: 'past', actual: 6100 },
      { date: '2026-07-29', zone: 'past', actual: 6125 },
    ];
    const { points, hasForecast } = normalizeForecastPoints(raw);
    expect(hasForecast).toBe(false);
    expect(points).toHaveLength(2);
    expect(points.every((p) => p.zone === 'past')).toBe(true);
    expect(points[1].base).toBeUndefined();
  });

  it('does not invent an anchor when there are no actuals at all', () => {
    const raw = [
      { date: '2026-08-04', zone: 'forecast', base: 6130, bull: 6200, bear: 6050 },
    ];
    const { points, hasForecast } = normalizeForecastPoints(raw);
    expect(hasForecast).toBe(true);
    expect(points.filter((p) => p.zone === 'today')).toHaveLength(0);
  });

  it('does not mutate the input array or point objects', () => {
    const raw = [
      { date: '2026-07-29', zone: 'past', actual: 6125 },
      { date: '2026-08-04', zone: 'forecast', base: 6130, bull: 6200, bear: 6050 },
    ];
    const snapshot = JSON.parse(JSON.stringify(raw));
    normalizeForecastPoints(raw);
    expect(raw).toEqual(snapshot);
  });
});
