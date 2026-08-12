import { formatMetric } from '../format';

export default function TableWidget({ cols, rows, onCellClick }) {
  const columns = cols?.length ? cols : Object.keys(rows?.[0] || {});
  return (
    <div className="max-h-72 overflow-auto rounded-md border border-border">
      <table className="w-full text-left text-sm">
        <thead className="sticky top-0 bg-secondary text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            {columns.map((col) => <th key={col} className="px-3 py-2 font-medium">{col}</th>)}
          </tr>
        </thead>
        <tbody className="divide-y divide-border bg-background">
          {(rows || []).map((row, rowIndex) => (
            <tr key={rowIndex} className="hover:bg-secondary/50">
              {columns.map((col) => (
                <td key={col} className="px-3 py-2 text-foreground">
                  <button
                    type="button"
                    className="max-w-[220px] truncate text-left hover:text-primary"
                    onClick={() => onCellClick?.(col, row[col])}
                    title={String(row[col] ?? '')}
                  >
                    {typeof row[col] === 'number' ? formatMetric(row[col], { compact: false }) : String(row[col] ?? '—')}
                  </button>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}