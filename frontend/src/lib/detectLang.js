/**
 * Detect whether a string is primarily Chinese or English.
 * Returns 'zh' if the text contains CJK characters, 'en' otherwise.
 * Returns null for empty/null input (caller should fall back to UI locale).
 */
export function detectLang(text) {
  if (!text || typeof text !== 'string' || !text.trim()) return null;
  const hasCJK = /[\u4e00-\u9fff]/.test(text);
  return hasCJK ? 'zh' : 'en';
}
