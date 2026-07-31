import { useState, useEffect, useCallback } from 'react';

const STORAGE_KEY = 'zhanlu_theme';

function readStoredTheme() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'light' || v === 'dark' || v === 'system') return v;
  } catch {}
  return 'system';
}

function resolveEffectiveTheme(stored) {
  if (stored === 'system') {
    if (typeof window !== 'undefined' && window.matchMedia) {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    return 'light';
  }
  return stored;
}

function applyTheme(resolved) {
  const root = document.documentElement;
  if (resolved === 'dark') root.classList.add('dark');
  else root.classList.remove('dark');
}

export function initTheme() {
  const stored = readStoredTheme();
  applyTheme(resolveEffectiveTheme(stored));
}

export function useTheme() {
  const [stored, setStoredState] = useState(readStoredTheme);
  const effective = resolveEffectiveTheme(stored);

  // Apply the effective theme to <html>
  useEffect(() => {
    applyTheme(effective);
  }, [effective]);

  // Listen for OS-level color-scheme changes when mode is 'system'
  useEffect(() => {
    if (stored !== 'system') return;
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    function onChange() {
      applyTheme(mq.matches ? 'dark' : 'light');
    }
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, [stored]);

  const setTheme = useCallback((next) => {
    setStoredState(next);
    try { localStorage.setItem(STORAGE_KEY, next); } catch {}
  }, []);

  return { theme: stored, setTheme };
}
