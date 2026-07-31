import { describe, it, expect } from 'vitest';
import { reorderWidgets } from './reorder';

describe('reorderWidgets', () => {
  it('moves widget from earlier to later position', () => {
    const ws = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
    expect(reorderWidgets(ws, 'a', 'c').map((w) => w.id)).toEqual(['b', 'c', 'a']);
  });

  it('moves widget from later to earlier position', () => {
    const ws = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
    expect(reorderWidgets(ws, 'c', 'a').map((w) => w.id)).toEqual(['c', 'a', 'b']);
  });

  it('no-op when same id', () => {
    const ws = [{ id: 'a' }, { id: 'b' }];
    expect(reorderWidgets(ws, 'a', 'a')).toBe(ws);
  });

  it('no-op when id not found', () => {
    const ws = [{ id: 'a' }, { id: 'b' }];
    expect(reorderWidgets(ws, 'x', 'a')).toBe(ws);
  });
});
