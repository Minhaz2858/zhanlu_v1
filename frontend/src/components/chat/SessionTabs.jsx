import { X } from 'lucide-react';

/**
 * SessionTabs — Kimi/GPT-style parallel conversation tabs.
 *
 * Renders the open sessions as a horizontal tab strip above the chat
 * content. Active tab is highlighted; closing a tab calls onClose with
 * the session id (the parent decides the fallback via closeTab()).
 */
export default function SessionTabs({ tabs = [], activeId = null, onSelect, onClose }) {
  if (!tabs || tabs.length === 0) return null;
  return (
    <div
      data-testid="session-tabs"
      className="flex items-center gap-1 overflow-x-auto border-b border-border bg-background/80 px-3 py-1.5"
    >
      {tabs.map((t) => (
        <div
          key={t.sessionId}
          data-testid={`session-tab-${t.sessionId}`}
          data-active={activeId === t.sessionId ? 'true' : 'false'}
          onClick={() => onSelect?.(t.sessionId)}
          className={`group flex max-w-52 shrink-0 cursor-pointer items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors ${
            activeId === t.sessionId
              ? 'border-primary/40 bg-primary/10 text-foreground'
              : 'border-border text-muted-foreground hover:bg-secondary'
          }`}
        >
          <span className="truncate">{t.title}</span>
          <button
            data-testid={`session-tab-close-${t.sessionId}`}
            onClick={(e) => {
              e.stopPropagation();
              onClose?.(t.sessionId);
            }}
            className="rounded p-0.5 text-muted-foreground/60 hover:bg-secondary hover:text-foreground"
            title="Close tab"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      ))}
    </div>
  );
}
