import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * MobileDetailShell — top bar with a back button wrapped around a detail
 * page (AgentConfig / ProjectDetail / ResourceDetail) so deep-linked detail
 * routes are reachable on mobile and don't fall through to the 404 page.
 *
 * The wrapped page is expected to be `h-full`; it sits below the bar in a
 * flex-1 region.
 */
export default function MobileDetailShell({ title, children }) {
  const { t } = useLanguage();
  const navigate = useNavigate();

  return (
    <div className="flex h-full flex-col bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b border-border bg-background px-3 py-2.5">
        <button
          onClick={() => navigate(-1)}
          className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-secondary"
          aria-label={t.mobile.back}
        >
          <ArrowLeft className="h-4 w-4" />
          <span className="max-w-[12rem] truncate">{title || t.mobile.back}</span>
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-hidden">
        {children}
      </div>
    </div>
  );
}
