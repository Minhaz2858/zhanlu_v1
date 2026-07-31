import { useCallback } from 'react';

/**
 * useTTS — Wrapper around browser SpeechSynthesis.
 * Provides speak(text, lang), pause(), resume(), cancel(), and speaking state.
 * Returns { speak, pause, resume, cancel, speaking }.
 */
export default function useTTS() {
  const speak = useCallback((text, lang = 'en-US') => {
    if (!window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = lang === 'zh' ? 'zh-CN' : lang;
    u.rate = 1.0;
    u.pitch = 1.0;
    u.volume = 1.0;
    window.speechSynthesis.speak(u);
  }, []);

  const pause = useCallback(() => {
    window.speechSynthesis?.pause();
  }, []);

  const resume = useCallback(() => {
    window.speechSynthesis?.resume();
  }, []);

  const cancel = useCallback(() => {
    window.speechSynthesis?.cancel();
  }, []);

  const speaking = typeof window !== 'undefined' ? window.speechSynthesis?.speaking || false : false;

  return { speak, pause, resume, cancel, speaking };
}
