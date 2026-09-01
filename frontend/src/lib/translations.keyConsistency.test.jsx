/**
 * zh/en key-structure consistency test for translations.js
 *
 * Regression guard (2026-08-13): the `en.settings.llmCatalog` object had 9
 * keys that were missing in `zh`, so Chinese users saw English fallback text
 * on the LLM model catalog page. This test walks the `zh` and `en` language
 * trees and asserts they have an identical key structure (same keys at the
 * same nesting level). A missing key in either language now fails loudly
 * instead of silently showing a fallback.
 *
 * Leaf values are compared structurally only (keys), not for exact wording —
 * the actual zh/en phrasing is intentionally different.
 */
import { describe, it, expect } from 'vitest';
import { translations } from './translations.js';

const LANGUAGES = ['zh', 'en'];

/**
 * Return the set of dot-paths to every leaf, e.g. { 'settings.llmCatalog.title', ... }.
 */
function collectLeafPaths(node, prefix = '', out = new Set()) {
  if (node === null || typeof node !== 'object' || Array.isArray(node)) {
    if (prefix) out.add(prefix);
    return out;
  }
  for (const key of Object.keys(node)) {
    const path = prefix ? `${prefix}.${key}` : key;
    collectLeafPaths(node[key], path, out);
  }
  return out;
}

describe('translations zh/en key-structure consistency', () => {
  const leafSets = {};
  for (const lang of LANGUAGES) {
    leafSets[lang] = collectLeafPaths(translations[lang]);
  }

  it('contains both zh and en language blocks', () => {
    for (const lang of LANGUAGES) {
      expect(translations[lang], `missing language block: ${lang}`).toBeTruthy();
    }
  });

  it('has no top-level keys present in only one language', () => {
    const zhTop = new Set(Object.keys(translations.zh));
    const enTop = new Set(Object.keys(translations.en));
    expect([...zhTop].filter((k) => !enTop.has(k))).toEqual([]);
    expect([...enTop].filter((k) => !zhTop.has(k))).toEqual([]);
  });

  it('has identical leaf key paths in zh and en', () => {
    const zhOnly = [...leafSets.zh].filter((k) => !leafSets.en.has(k));
    const enOnly = [...leafSets.en].filter((k) => !leafSets.zh.has(k));
    // Report missing keys grouped by language for actionable output.
    expect(zhOnly, `keys present in zh but missing in en: ${zhOnly.join(', ')}`).toEqual([]);
    expect(enOnly, `keys present in en but missing in zh: ${enOnly.join(', ')}`).toEqual([]);
  });

  it('leaves no leaf value as undefined in either language', () => {
    for (const lang of LANGUAGES) {
      for (const path of leafSets[lang]) {
        const node = path.split('.').reduce((acc, k) => (acc == null ? acc : acc[k]), translations[lang]);
        expect(node, `${lang}.${path} resolved to undefined`).not.toBeUndefined();
      }
    }
  });
});
