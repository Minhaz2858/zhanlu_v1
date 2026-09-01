import { useState } from 'react';
import { History, Plus } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import HistorySheet from './HistorySheet';

/**
 * MobileTopBar — sticky top bar for the mobile layout.
 *
 * The brand/history affordance sits at the TOP-LEFT (per the plan:
 * "历史记录缩到左上角"). Tapping it opens the HistorySheet drawer. An
 * optional trailing action (e.g. "+ New Chat") renders on the right.
 */
export default function MobileTopBar({ title, showNewChat, onNewChat }) {
  const { t } = useLanguage();
  const [historyOpen, setHistoryOpen] = useState(false);

  return (
    <header className="flex shrink-0 items-center justify-between gap-2 border-b border-border bg-background px-3 py-2.5">
      <button
        onClick={() => setHistoryOpen(true)}
        className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-secondary"
        aria-label={t.mobile.history}
      >
        <History className="h-4 w-4" />
        <span className="max-w-[8rem] truncate">{title || t.mobile.chat}</span>
      </button>
      <div className="flex items-center gap-1">
        {showNewChat && (
          <button
            onClick={onNewChat}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground"
          >
            <Plus className="h-3.5 w-3.5" /> {t.mobile.newChat}
          </button>
        )}
      </div>
      <HistorySheet open={historyOpen} onClose={() => setHistoryOpen(false)} />
    </header>
  );
}
