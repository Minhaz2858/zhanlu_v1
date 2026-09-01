import { useNavigate } from 'react-router-dom';
import { useLanguage } from '@/lib/LanguageProvider';
import { Plus, MessageSquare, Trash2, ArrowLeft } from 'lucide-react';

export default function BuilderConversationList({ conversations, activeId, onSelect, onNew, onDelete, backTo = '/my-space', labels = {} }) {
  const { t } = useLanguage();
  const navigate = useNavigate();
  const L = { newChat: labels.newChat || t.agentBuilder.newChat, noChats: labels.noChats || t.agentBuilder.noChats, untitled: labels.untitled || t.agentBuilder.untitled };

  return (
    <div className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-sidebar">
      <div className="flex items-center justify-between gap-2 px-3 py-3">
        <button onClick={() => navigate(backTo)} className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground">
          <ArrowLeft className="h-4 w-4" />
        </button>
        <button onClick={onNew} className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90">
          <Plus className="h-3.5 w-3.5" /> {L.newChat}
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {conversations.length === 0 ? (
          <p className="px-2 py-4 text-center text-xs text-muted-foreground">{L.noChats}</p>
        ) : (
          conversations.map((c) => (
            <div key={c.id} onClick={() => onSelect(c.id)} className={`group mb-1 flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-xs transition-colors ${activeId === c.id ? 'bg-sidebar-accent text-foreground' : 'text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground'}`}>
              <MessageSquare className="h-3.5 w-3.5 shrink-0" />
              <span className="flex-1 truncate">{c.metadata?.name || L.untitled}</span>
              <button onClick={(e) => { e.stopPropagation(); onDelete(c.id); }} className="opacity-0 transition-opacity group-hover:opacity-100 hover:text-destructive">
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}