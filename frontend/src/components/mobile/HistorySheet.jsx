import { useNavigate } from 'react-router-dom';
import { Plus, Trash2, MessageSquare, X } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { useChatSession } from '@/lib/ChatSessionContext';
import { formatRelativeTime } from '@/lib/time';

/**
 * HistorySheet — mobile history drawer opened from the top-left corner.
 *
 * Replaces the desktop sidebar's SessionList. Reads the same
 * ChatSessionContext so selecting a session / starting a new chat keeps
 * the desktop and mobile experiences in sync. Rendered as a left-side
 * sliding sheet; tapping a session navigates to /chat and closes.
 */
export default function HistorySheet({ open, onClose }) {
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const { sessions, activeId, selectSession, newChat, deleteSession } = useChatSession();

  if (!open) return null;

  function handleSelect(id) {
    selectSession(id);
    onClose();
    navigate('/chat');
  }

  function handleNew() {
    newChat(null);
    onClose();
    navigate('/chat');
  }

  return (
    <div className="fixed inset-0 z-50">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      {/* Sheet */}
      <div className="absolute inset-y-0 left-0 flex w-[82%] max-w-xs flex-col bg-background shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <MessageSquare className="h-4 w-4 text-primary" />
            <span className="font-display text-base text-foreground">{t.mobile.history}</span>
          </div>
          <button
            onClick={handleNew}
            className="inline-flex items-center gap-1 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground"
          >
            <Plus className="h-3.5 w-3.5" /> {t.mobile.newChat}
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2">
          {sessions.length === 0 ? (
            <p className="px-4 py-8 text-center text-xs text-muted-foreground">{t.mobile.noSessions}</p>
          ) : (
            sessions.map((s) => (
              <div
                key={s.id}
                onClick={() => handleSelect(s.id)}
                className={`mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2.5 transition-colors ${
                  activeId === s.id ? 'bg-primary/10' : 'hover:bg-secondary'
                }`}
              >
                <MessageSquare className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="flex-1 truncate text-sm text-foreground">{s.title}</span>
                {s.updated_date && (
                  <span className="shrink-0 text-[10px] text-muted-foreground/60">
                    {formatRelativeTime(s.updated_date, lang)}
                  </span>
                )}
                <button
                  onClick={(e) => { e.stopPropagation(); deleteSession(s); }}
                  className="shrink-0 rounded p-1 text-muted-foreground/40 hover:text-destructive"
                  aria-label={t.common.delete}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))
          )}
        </div>

        <div className="border-t border-border p-2">
          <button
            onClick={onClose}
            className="flex w-full items-center justify-center gap-1 rounded-lg py-2 text-sm text-muted-foreground hover:bg-secondary"
          >
            <X className="h-4 w-4" /> {t.mobile.back}
          </button>
        </div>
      </div>
    </div>
  );
}
