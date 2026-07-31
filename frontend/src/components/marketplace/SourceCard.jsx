import { Plus, Loader2 } from 'lucide-react';

/**
 * SourceCard — a single source on the Browse Marketplace source row.
 *
 * Visual contract: a neutral dark card with a small monochrome icon on
 * the left and the source name + skill count on the right. No brand
 * colors — the design is deliberately plain so the cards read as
 * navigation, not as a marketing surface. Active state is a 1px
 * primary border, nothing more.
 *
 * The "+" variant is rendered for the trailing "Add Source" trigger
 * so the row stays visually uniform.
 */
export default function SourceCard({ source, isActive, onClick, onAdd, kind, addLabel }) {
  if (kind === 'add' || source?.kind === 'add') {
    return (
      <button
        type="button"
        onClick={onAdd}
        title={addLabel || 'Add source'}
        className="flex h-[56px] w-[120px] shrink-0 items-center justify-center gap-1.5 rounded-lg border border-dashed border-border bg-card text-xs text-muted-foreground transition hover:border-foreground/40 hover:text-foreground"
      >
        <Plus className="h-3.5 w-3.5" />
        {addLabel || 'Add source'}
      </button>
    );
  }

  const glyph = source?.icon_emoji || (source?.name?.[0] || '?').toUpperCase();
  const skillCount = source?.skill_count ?? 0;
  const isSyncing = source?.last_sync_status === 'syncing';
  const neverSynced = source?.last_sync_status === 'never' || source?.last_sync_status == null;

  return (
    <button
      type="button"
      onClick={() => onClick?.(source)}
      title={source?.description || source?.name}
      className={`flex h-[56px] w-[170px] shrink-0 items-center gap-2.5 rounded-lg border bg-card px-3 text-left transition ${
        isActive
          ? 'border-primary'
          : 'border-border hover:border-foreground/30'
      }`}
    >
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-secondary text-sm font-medium text-foreground">
        {glyph}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-medium text-foreground">{source?.name}</span>
        <span className="flex items-center gap-1 truncate text-[10px] text-muted-foreground">
          {isSyncing ? (
            <>
              <Loader2 className="h-2.5 w-2.5 animate-spin shrink-0" />
              <span>Syncing…</span>
            </>
          ) : neverSynced && skillCount === 0 ? (
            <span>Will sync on first load</span>
          ) : (
            <span>
              {skillCount} {skillCount === 1 ? 'skill' : 'skills'}
            </span>
          )}
        </span>
      </span>
    </button>
  );
}
