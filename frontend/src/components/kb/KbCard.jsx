import { Database, FileText, Pause, Play, Pencil, Trash2, RefreshCw } from 'lucide-react';

export default function KbCard({ item, t, translate, onClick, onEdit, onTogglePause, onDelete, onReindex }) {
  if (!item) return null;
  const isFile = item.source_kind === 'file';
  const Icon = isFile ? FileText : Database;
  const paused = item.status === 'paused';
  const sub = isFile
    ? (t?.kb?.fileTypes?.[item.file_type] || item.file_type || t?.kb?.sourceKinds?.file)
    : (t?.kb?.dbTypes?.[item.db_type] || item.db_type || t?.kb?.sourceKinds?.database);
  const idxStatus = item.indexing_status;
  const idxLabel = {
    pending: t.kb?.idxPending || 'Queued',
    indexing: t.kb?.idxIndexing || 'Indexing…',
    ready: t.kb?.idxReady || 'Indexed',
    failed: t.kb?.idxFailed || 'Index failed',
  }[idxStatus] || (isFile ? (t.kb?.idxNone || 'Not indexed') : '');

  return (
    <div onClick={onClick} className="group flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm">
      <div className="mb-2 flex items-start gap-2">
        <Icon className="mt-0.5 h-4 w-4 text-primary" />
        <h3 className="min-w-0 flex-1 break-words font-display text-base text-foreground group-hover:text-primary">{translate(item.name)}</h3>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${paused ? 'bg-secondary text-muted-foreground' : 'bg-primary/10 text-primary'}`}>{t.detail.kbStatuses[item.status] || item.status}</span>
      </div>
      <p className="mb-1 flex-1 break-words text-xs text-muted-foreground">{item.description ? translate(item.description) : sub}</p>
      {isFile && idxLabel && (
        <div className="mb-2 flex items-center gap-2 text-[10px] text-muted-foreground">
          <span className={`rounded px-1.5 py-0.5 ${idxStatus === 'ready' ? 'bg-primary/10 text-primary' : idxStatus === 'failed' ? 'bg-destructive/10 text-destructive' : 'bg-secondary'}`}>{idxLabel}</span>
          {item.chunk_count ? <span>{item.chunk_count} {t.kb?.chunks || 'chunks'}</span> : null}
        </div>
      )}
      <div className="mt-3 flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
        <span className="rounded bg-secondary/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">{sub}</span>
        <div className="ml-auto flex gap-2">
          {isFile && (
            <button onClick={() => onReindex?.(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary" title={t.kb?.reindex || 'Reindex'}>
              <RefreshCw className="h-3 w-3" /> {t.kb?.reindex || 'Reindex'}
            </button>
          )}
          <button onClick={() => onTogglePause(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary">
            {paused ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />} {paused ? t.kb.resume : t.kb.pause}
          </button>
          <button onClick={() => onEdit(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary">
            <Pencil className="h-3 w-3" /> {t.kb.edit}
          </button>
          <button onClick={() => onDelete(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-destructive">
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      </div>
    </div>
  );
}