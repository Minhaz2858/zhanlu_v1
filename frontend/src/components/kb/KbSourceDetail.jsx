import { Database, FileText, Pause, Play, Pencil, Download } from 'lucide-react';

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="truncate text-foreground">{value}</span>
    </div>
  );
}

export default function KbSourceDetail({ item, t, onEdit, onTogglePause }) {
  if (!item) return null;
  const isFile = item.source_kind === 'file';
  const Icon = isFile ? FileText : Database;
  const paused = item.status === 'paused';

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-primary" />
          <h3 className="font-display text-base text-foreground">{t?.kb?.connected}</h3>
        </div>
        <span className={`rounded-full px-2.5 py-0.5 text-xs ${paused ? 'bg-secondary text-muted-foreground' : 'bg-primary/10 text-primary'}`}>{t?.detail?.kbStatuses?.[item.status] || item.status}</span>
      </div>
      <div className="space-y-2.5 text-sm">
        {isFile ? (
          <>
            <Row label={t?.kb?.fileTypes?.[item.file_type] || t?.kb?.sourceKinds?.file} value={item.file_url ? decodeURIComponent(item.file_url.split('/').pop() || item.file_url) : '—'} />
            {item.file_url && <a href={item.file_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm text-primary hover:underline"><Download className="h-3.5 w-3.5" /> {t?.detail?.download}</a>}
          </>
        ) : item.db_type === 'api' ? (
          <Row label={t?.kb?.dbTypes?.api} value={item.api_url || '—'} />
        ) : (
          <>
            <Row label={t?.kb?.dbTypes?.[item.db_type] || item.db_type} value={`${item.host || '—'}${item.port ? ':' + item.port : ''}`} />
            <Row label={t?.kb?.database} value={item.database_name || '—'} />
            <Row label={t?.kb?.username} value={item.username || '—'} />
          </>
        )}
        {item.item_count != null && <Row label={t?.detail?.itemCount} value={item.item_count} />}
      </div>
      <div className="mt-4 flex gap-2">
        <button onClick={() => onTogglePause?.(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-secondary">
          {paused ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />} {paused ? t?.kb?.resume : t?.kb?.pause}
        </button>
        <button onClick={() => onEdit?.(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-secondary">
          <Pencil className="h-3 w-3" /> {t?.kb?.edit}
        </button>
      </div>
    </div>
  );
}