import useIsMobileCanonical from "./useIsMobile"

// Backward-compatible mobile hook.
//
// Historically this file only did a width check (< 768px). As part of the
// mobile-adaptive-layout plan the project unified on a dual-condition
// device check (width ≤ 1024px AND touch capability) so the app only
// switches to the mobile UI on a genuine phone — never on a shrunk
// desktop window. Existing callers import `useIsMobile` from this module;
// we keep that export name and delegate to the canonical implementation.
export function useIsMobile() {
  return useIsMobileCanonical(1024);
}
