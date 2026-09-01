import { createContext, useContext, useEffect, useMemo, useState } from 'react';

/**
 * mobileMode — context that lets a desktop user force the mobile UI for
 * debugging/QA (mirrors the `forceMobile` toggle in the xincheng project).
 *
 * When `forceMobile` is true, the app renders the mobile layout regardless
 * of the real device detection, wrapped in a phone frame (see MobileFrame)
 * so it can be inspected on a desktop browser.
 *
 * Exposes:
 *   - forceMobile: boolean
 *   - setForceMobile(next): manually toggle
 */
const MobileModeContext = createContext({ forceMobile: false, setForceMobile: () => {} });

const STORAGE_KEY = 'zhanlu_force_mobile';

function readStored() {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

export function MobileModeProvider({ children }) {
  const [forceMobile, setForceMobileState] = useState(readStored);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, forceMobile ? '1' : '0');
    } catch { /* storage unavailable — best effort */ }
  }, [forceMobile]);

  const value = useMemo(() => ({
    forceMobile,
    setForceMobile: setForceMobileState,
  }), [forceMobile]);

  return (
    <MobileModeContext.Provider value={value}>
      {children}
    </MobileModeContext.Provider>
  );
}

export function useMobileMode() {
  return useContext(MobileModeContext);
}
