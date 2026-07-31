import { X } from 'lucide-react';
import TableWidget from './widgets/TableWidget';

// Inline drill-down detail panel shown beneath the source widget. Renders the
// __drill__ query result (run with :drill_value set to the clicked cell) as a
// table. The viewer mounts this only when a __drill__ result is present and
// its source_widget_id matches the widget above it.
export default function DrillPanel({ result, onClose }) {
  const rows = result?.rows || [];
  const cols = result?.columns || [];
  const err = result?.error;
  const value = result?.drill_value;
  return (
    <div className="rounded-xl border border-border bg-card p-3 shadow-sm">
      <div className="mb-2 flex items-center justify-between">
        <p className="text-xs font-medium text-muted-foreground">
          Drill: {value}
          {rows.length ? ` · ${rows.length} row${rows.length === 1 ? '' : 's'}` : ''}
        </p>
        <button
          onClick={onClose}
          aria-label="Close drill"
          className="rounded-md p-1 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      {err ? (
        <p className="text-xs text-destructive">{err}</p>
      ) : rows.length ? (
        <TableWidget cols={cols} rows={rows} />
      ) : (
        <p className="text-xs text-muted-foreground">No rows.</p>
      )}
    </div>
  );
}
