import { Bot, Zap, FileText, LayoutDashboard, FileCode, Database, Pin, Pencil, Trash2 } from 'lucide-react';

const KIND_ICON = {
  chatbot: Bot,
  automation_result: Zap,
  report: FileText,
  dashboard: LayoutDashboard,
  html_file: FileCode,
  document: FileText,
  data_file: Database,
};

export default function FileCard({ item, t, translate, timeLabel, onOpen, onPin, onRename, onDelete }) {
  const kind = item.resource_kind || 'document';
  const Icon = KIND_ICON[kind] || FileText;
  const unread = item.read === false;
  const isAuto = kind === 'automation_result';
  const pinned = item.pinned === true;

  return (
    <div
      onClick={() => onOpen(item)}
      className={`group flex cursor-pointer items-center gap-3 rounded-xl border bg-card p-4 transition-colors hover:border-primary/30 ${unread ? 'border-primary/40' : 'border-border'}`}
    >
      <div className="relative shrink-0">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
          <Icon className="h-4 w-4 text-primary" />
        </div>
        {unread && <span className="absolute -right-1 -top-1 h-3 w-3 rounded-full bg-primary ring-2 ring-card" />}
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h3 className={`truncate text-sm ${unread ? 'font-semibold text-foreground' : 'font-medium text-foreground'}`}>
            {translate(item.name)}
          </h3>
          {isAuto && unread && (
            <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">{t.myFiles.unread}</span>
          )}
          {pinned && <Pin className="h-3 w-3 shrink-0 text-primary" />}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span className="rounded bg-secondary/60 px-1.5 py-0.5">{t.myFiles.kinds[kind]}</span>
          {item.project && <span className="truncate">{item.project}</span>}
          {item.agent_name && <span className="truncate">· {item.agent_name}</span>}
        </div>
      </div>

      <span className="hidden shrink-0 text-xs text-muted-foreground sm:block">{timeLabel}</span>

      <div className="flex shrink-0 items-center gap-0.5" onClick={(e) => e.stopPropagation()}>
        <button onClick={() => onPin(item)} title={pinned ? t.myFiles.unpin : t.myFiles.pin} className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground">
          <Pin className={`h-3.5 w-3.5 ${pinned ? 'fill-primary text-primary' : ''}`} />
        </button>
        <button onClick={() => onRename(item)} title={t.myFiles.rename} className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground">
          <Pencil className="h-3.5 w-3.5" />
        </button>
        <button onClick={() => onDelete(item)} title={t.myFiles.delete} className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-destructive">
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}