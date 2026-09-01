import { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { translations } from './translations';

const LanguageContext = createContext({ lang: 'zh', setLang: () => {}, aiLang: 'auto', setAiLang: () => {}, t: translations.zh });

const STORAGE_KEY = 'zhanlu_lang';
const AI_STORAGE_KEY = 'zhanlu_ai_lang';

function readStoredLang() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'zh' || v === 'en') return v;
  } catch {}
  return 'zh';
}

function readStoredAiLang() {
  try {
    const v = localStorage.getItem(AI_STORAGE_KEY);
    if (v === 'zh' || v === 'en' || v === 'auto') return v;
  } catch {}
  return 'auto';
}

export function LanguageProvider({ children }) {
  const [lang, setLangState] = useState(readStoredLang);
  const [aiLang, setAiLangState] = useState(readStoredAiLang);

  // Restore UI language from server on first visit (cross-device sync)
  useEffect(() => {
    if (localStorage.getItem(STORAGE_KEY)) return;
    base44.entities.UserSetting.list('', 1)
      .then((list) => { if (list.length > 0 && list[0].language) setLangState(list[0].language); })
      .catch(() => {});
  }, []);

  // Restore AI output language from server on first visit
  useEffect(() => {
    if (localStorage.getItem(AI_STORAGE_KEY)) return;
    base44.entities.UserSetting.list('', 1)
      .then((list) => { if (list.length > 0 && list[0].ai_language) setAiLangState(list[0].ai_language); })
      .catch(() => {});
  }, []);

  // Keep the document-level `lang` attribute in sync with the UI language
  // so screen readers, spell-checkers and search engines see the correct
  // locale. `index.html` hardcodes `lang="en"`, so we always override here.
  useEffect(() => {
    try {
      document.documentElement.setAttribute('lang', lang === 'en' ? 'en' : 'zh-CN');
    } catch { /* noop */ }
  }, [lang]);

  const setLang = useCallback(async (next) => {
    setLangState(next);
    try { localStorage.setItem(STORAGE_KEY, next); } catch {}
    try {
      const list = await base44.entities.UserSetting.list('', 1);
      if (list.length > 0) await base44.entities.UserSetting.update(list[0].id, { language: next });
      else await base44.entities.UserSetting.create({ language: next });
    } catch { /* noop */ }
  }, []);

  const setAiLang = useCallback(async (next) => {
    setAiLangState(next);
    try { localStorage.setItem(AI_STORAGE_KEY, next); } catch {}
    try {
      const list = await base44.entities.UserSetting.list('', 1);
      if (list.length > 0) await base44.entities.UserSetting.update(list[0].id, { ai_language: next });
      else await base44.entities.UserSetting.create({ ai_language: next });
    } catch { /* noop */ }
  }, []);

  return (
    <LanguageContext.Provider value={{ lang, setLang, aiLang, setAiLang, t: translations[lang] || translations.zh }}>
      {children}
    </LanguageContext.Provider>
  );
}

export function useLanguage() {
  return useContext(LanguageContext);
}