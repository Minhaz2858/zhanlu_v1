import { describe, it, expect } from 'vitest';
import {
  ENTERPRISE_AREAS, ENTERPRISE_TABS,
  parseEnterpriseView, buildEnterpriseSearch,
} from './enterpriseUrlState';

describe('enterpriseUrlState (Phase 6F deep-links)', () => {
  it('covers every tab exactly once across the six areas', () => {
    const keys = ENTERPRISE_AREAS.flatMap((a) => a.tabs);
    expect(new Set(keys).size).toBe(keys.length);
    expect(Object.keys(ENTERPRISE_TABS).sort()).toEqual([...keys].sort());
    expect(ENTERPRISE_AREAS).toHaveLength(6);
  });

  it('parses a full deep link', () => {
    const params = new URLSearchParams('exp=K&object=o1&resource=kb-1&rule=br9');
    expect(parseEnterpriseView(params)).toEqual({
      exp: 'K', objectId: 'o1', resourceId: 'kb-1', ruleId: 'br9',
    });
  });

  it('defaults to the Enterprise Map and drops invalid refs', () => {
    expect(parseEnterpriseView(new URLSearchParams())).toEqual({
      exp: 'M', objectId: null, resourceId: null, ruleId: null,
    });
    expect(parseEnterpriseView(new URLSearchParams('exp=ZZZ&object=%20'))
      .exp).toBe('M');
  });

  it('admin-only tabs fall back for non-admins, pass for admins', () => {
    const p = new URLSearchParams('exp=R');
    expect(parseEnterpriseView(p).exp).toBe('M');
    expect(parseEnterpriseView(p, { adminKeys: ['R'] }).exp).toBe('R');
  });

  it('round-trips: build omits defaults and nulls', () => {
    const view = { exp: 'C', objectId: 'o1', resourceId: null, ruleId: null };
    const s = buildEnterpriseSearch(view);
    expect(s).toBe('exp=C&object=o1');
    const round = parseEnterpriseView(new URLSearchParams(s));
    expect(round).toEqual(view);
    expect(buildEnterpriseSearch({ exp: 'M', objectId: null, resourceId: null, ruleId: null }))
      .toBe('');
  });
});
