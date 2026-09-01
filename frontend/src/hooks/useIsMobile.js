import { useEffect, useState } from 'react';

/**
 * useIsMobile — dual-condition mobile device detection.
 *
 * A device is treated as "mobile" ONLY when BOTH:
 *   1. the viewport width is ≤ `breakpoint` (default 1024px), AND
 *   2. the device supports touch input.
 *
 * Touch capability is detected ONCE at init (`'ontouchstart' in window`
 * or `navigator.maxTouchPoints > 0`) and never re-evaluated, so a PC
 * that has touch hardware (e.g. a touch-screen laptop) still counts as
 * touch-capable — but because it is usually wide, the width gate keeps
 * it desktop. Conversely, a desktop window squeezed below 1024px on a
 * touchless monitor never counts as mobile because `hasTouch` is false.
 *
 * This mirrors the proven mechanism from the xincheng project (方案A of
 * the mobile-adaptive-layout plan): the app only switches to the mobile
 * UI on a genuine phone, never on a shrunk desktop window.
 *
 * The width gate uses `matchMedia('(max-width: <bp>px)')` so it reacts
 * to width changes (e.g. rotating a phone, resizing a hybrid window),
 * but the touch gate is frozen after mount.
 *
 * @param {number} breakpoint - max viewport width (in px) that counts as mobile.
 * @returns {boolean} true when the device is a genuine mobile device.
 */
export default function useIsMobile(breakpoint = 1024) {
  const [isNarrow, setIsNarrow] = useState(() => {
    if (typeof window === 'undefined') return false;
    return window.matchMedia(`(max-width: ${breakpoint}px)`).matches;
  });

  // Touch capability is only meaningful on the first mount; freezing it
  // in a ref keeps it stable across the component's lifetime.
  const [hasTouch] = useState(() => {
    if (typeof window === 'undefined') return false;
    return ('ontouchstart' in window) || ((navigator.maxTouchPoints || 0) > 0);
  });

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const mql = window.matchMedia(`(max-width: ${breakpoint}px)`);
    const onChange = () => setIsNarrow(mql.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [breakpoint]);

  return isNarrow && hasTouch;
}
