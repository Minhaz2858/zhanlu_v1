import { formatMetric } from '../format';

export default function GaugeWidget({ widget, rows }) {
  const options = widget.options || {};
  const row = rows?.[0] || {};
  const valueKey = options.value_column || Object.keys(row)[0];
  const min = Number.isFinite(Number(options.min)) ? Number(options.min) : 0;
  const max = Number.isFinite(Number(options.max)) ? Number(options.max) : 100;
  const value = Number(row[valueKey]);
  const pct = Number.isFinite(value) && max !== min ? Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100)) : 0;
  return (
    <div className="space-y-4">
      <div className="text-3xl font-semibold text-foreground">
        {formatMetric(row[valueKey], { unit: options.unit })}
      </div>
      <div className="h-3 overflow-hidden rounded-full bg-secondary">
        <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
      </div>
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>{formatMetric(min, { unit: options.unit })}</span>
        {options.target != null && <span>Target {formatMetric(options.target, { unit: options.unit })}</span>}
        <span>{formatMetric(max, { unit: options.unit })}</span>
      </div>
    </div>
  );
}