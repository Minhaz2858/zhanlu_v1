/**
 * Unit tests for the completion-notification decision helpers
 * (frontend/src/lib/completionNotify.js).
 */
import { describe, it, expect } from 'vitest';
import { shouldNotify, notificationBody } from './completionNotify';

describe('shouldNotify', () => {
  it('notifies only when the tab is hidden', () => {
    expect(shouldNotify({ hidden: true, permission: 'granted', hasFinalMessage: true })).toBe(true);
    expect(shouldNotify({ hidden: false, permission: 'granted', hasFinalMessage: true })).toBe(false);
  });

  it('requires granted notification permission', () => {
    expect(shouldNotify({ hidden: true, permission: 'denied', hasFinalMessage: true })).toBe(false);
    expect(shouldNotify({ hidden: true, permission: 'default', hasFinalMessage: true })).toBe(false);
    expect(shouldNotify({ hidden: true, permission: undefined, hasFinalMessage: true })).toBe(false);
  });

  it('requires a final assistant message', () => {
    expect(shouldNotify({ hidden: true, permission: 'granted', hasFinalMessage: false })).toBe(false);
  });

  it('is false when nothing is provided', () => {
    expect(shouldNotify({})).toBe(false);
  });
});

describe('notificationBody', () => {
  it('truncates long messages to the max length', () => {
    const body = notificationBody('x'.repeat(500), 200);
    expect(body.length).toBe(200);
    expect(body.endsWith('…')).toBe(true);
  });

  it('keeps short messages intact', () => {
    expect(notificationBody('done', 200)).toBe('done');
  });

  it('normalizes whitespace and falls back to a default', () => {
    expect(notificationBody('  lots   of   space  ', 200)).toBe('lots of space');
    expect(notificationBody('', 200)).toBe('Task finished');
    expect(notificationBody(null, 200)).toBe('Task finished');
    expect(notificationBody(undefined, 200)).toBe('Task finished');
  });

  it('strips markdown code fences for readability', () => {
    expect(notificationBody('```js\nconst a = 1;\n```', 200)).not.toContain('```');
  });
});
