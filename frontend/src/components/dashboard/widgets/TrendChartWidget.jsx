import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { formatAxisTick } from '../format';

export default function TrendChartWidget({ kind = 'line', widget, rows, onCellClick }) {
  const options = widget.options || {};
  const xKey = options.x_column || Object.keys(rows?.[0] || {})[0];
  const yKey = options.y_column || Object.keys(rows?.[0] || {})[1];
  const Chart = kind === 'bar' ? BarChart : LineChart;
  const Series = kind === 'bar' ? Bar : Line;

  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <Chart data={rows || []} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey={xKey} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tickFormatter={formatAxisTick} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={48} />
          <Tooltip />
          <Series
            dataKey={yKey}
            fill="hsl(var(--primary))"
            stroke="hsl(var(--primary))"
            strokeWidth={2}
            radius={kind === 'bar' ? [4, 4, 0, 0] : undefined}
            dot={kind === 'line' ? { r: 2 } : undefined}
            onClick={(entry) => onCellClick?.(xKey, entry?.payload?.[xKey])}
          />
        </Chart>
      </ResponsiveContainer>
    </div>
  );
}