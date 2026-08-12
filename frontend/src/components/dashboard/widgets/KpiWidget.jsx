import { ArrowDownRight, ArrowRight, ArrowUpRight } from 'lucide-react';
import { formatDelta, formatMetric } from '../format';

export default function KpiWidget({ widget, rows }) {
  const row = rows?.[0] || {};
  const options = widget.options || {};
  const valueColumn = options.value_column || Object.keys(row)[0];
  const compareColumn = options.compare_column;
  const value = row[valueColumn];
  const delta = compareColumn ? formatDelta(value, row[compareColumn]) : null;
  const DeltaIcon = delta?.direction === 'up' ? ArrowUpRight : delta?.direction === 'down' ? ArrowDownRight : ArrowRight;
  const deltaClass = delta?.direction === 'down'
    ? 'text-destructive bg-destructive/10'
    : delta?.direction === 'up'
      ? 'text-[hsl(var(--chart-2))] bg-[hsl(var(--chart-2))]/10'
      : 'text-muted-foreground bg-secondary';

  return (
    <div className="space-y-3">
      <div className="text-3xl font-semibold tracking-normal text-foreground">
        {formatMetric(value, { unit: options.unit })}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {delta && (
          <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${deltaClass}`}>
            <DeltaIcon className="h-3 w-3" />
            {delta.pct}%
          </span>
        )}
        {options.subtitle && <span className="text-xs text-muted-foreground">{options.subtitle}</span>}
      </div>
    </div>
  );
}