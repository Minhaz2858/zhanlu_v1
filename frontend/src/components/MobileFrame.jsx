import { useMobileMode } from '@/lib/mobileMode';

/**
 * MobileFrame — responsive shell for the mobile UI.
 *
 * On a real phone it renders its children full-screen (100dvh). When the
 * desktop user has toggled `forceMobile` (for debugging/QA), it wraps the
 * children in a 430×860 "phone" viewport so the mobile layout is
 * inspectable on a desktop browser. In both modes the frame provides the
 * definitive height; inner components (e.g. MobileLayout) use `h-full` so
 * they resolve against this frame rather than the window.
 */
export default function MobileFrame({ children }) {
  const { forceMobile } = useMobileMode();

  if (!forceMobile) {
    // Real mobile device: fill the viewport.
    return (
      <div className="h-[100dvh] w-full overflow-hidden bg-background">
        {children}
      </div>
    );
  }

  // Desktop forced-mode: phone frame. The frame is a fixed 430×860 box;
  // children fill it via h-full.
  return (
    <div className="flex h-screen w-full items-center justify-center bg-slate-300/40 p-6">
      <div className="relative flex h-[860px] w-[430px] flex-col overflow-hidden rounded-[2.5rem] border border-slate-300 bg-background shadow-2xl">
        {children}
      </div>
    </div>
  );
}
