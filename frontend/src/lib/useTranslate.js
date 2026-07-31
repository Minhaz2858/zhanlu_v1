import { useState, useEffect } from 'react';
import { base44 } from '@/api/base44Client';

const cache = new Map();
const inflight = new Set();
const k = (text, lang) => `${lang}::${text}`;

// Detect whether text already matches the target language.
// zh target: text is considered Chinese if it contains CJK characters.
// en target: text is considered English if it has no CJK characters.
function isTargetLang(text, lang) {
  if (!text || typeof text !== 'string') return true;
  const hasCJK = /[\u4e00-\u9fff]/.test(text);
  return lang === 'zh' ? hasCJK : !hasCJK;
}

export function getCached(text, lang) {
  if (!text) return text;
  if (isTargetLang(text, lang)) return text;
  return cache.get(k(text, lang)) || null;
}

export async function batchTranslate(texts, lang) {
  const unique = [...new Set(texts.filter((t) => t && typeof t === 'string'))];
  const missing = unique.filter((t) => !isTargetLang(t, lang) && !cache.has(k(t, lang)) && !inflight.has(k(t, lang)));
  if (missing.length === 0) return;
  missing.forEach((t) => inflight.add(k(t, lang)));
  try {
    const res = await base44.integrations.Core.InvokeLLM({
      prompt: `You are a professional translator. Translate each text in the input JSON array into ${lang === 'en' ? 'English' : 'Simplified Chinese'}. Return a JSON object: { "translations": [...] } where the array contains the translations in the EXACT SAME ORDER and SAME LENGTH as the input. Preserve proper nouns, numbers, dates, and formatting. Do not add explanations.\n\nInput:\n${JSON.stringify(missing)}`,
      response_json_schema: {
        type: 'object',
        properties: {
          translations: {
            type: 'array',
            items: { type: 'string' },
          },
        },
        required: ['translations'],
      },
    });
    const translations = Array.isArray(res?.translations) ? res.translations : [];
    missing.forEach((t, i) => {
      const tr = translations[i];
      cache.set(k(t, lang), (tr && typeof tr === 'string' && tr.trim()) ? tr : t);
    });
  } catch {
    missing.forEach((t) => cache.set(k(t, lang), t));
  } finally {
    missing.forEach((t) => inflight.delete(k(t, lang)));
  }
}

export function useTranslate(texts, lang) {
  const [, force] = useState(0);
  const key = (texts || []).filter(Boolean).join('\u0001');
  useEffect(() => {
    const missing = [...new Set((texts || []).filter(Boolean))].filter((t) => !isTargetLang(t, lang) && !getCached(t, lang));
    if (missing.length === 0) return;
    let active = true;
    batchTranslate(missing, lang).then(() => { if (active) force((n) => n + 1); });
    return () => { active = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, lang]);
  return (text) => getCached(text, lang) || text;
}