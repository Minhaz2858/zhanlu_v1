import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { formatAxisTick } from '../format';

const COLORS = ['hsl(var(--primary))', 'hsl(var(--chart-2))', 'hsl(var(--chart-3))', 'hsl(var(--chart-4))'];

export default function StackedBarWidget({ widget, rows, onCellClick }) {
  const options = widget.options || {};
  const xKey = options.x_column || Object.keys(rows?.[0] || {})[0];
  const series = options.series?.length ? options.series : Object.keys(rows?.[0] || {}).filter((key) => key !== xKey).map((key) => ({ key, label: key }));
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows || []} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey={xKey} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tickFormatter={formatAxisTick} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={48} />
          <Tooltip />
          <Legend />
          {series.map((item, index) => (
            <Bar key={item.key} dataKey={item.key} name={item.label || item.key} stackId="total" fill={COLORS[index % COLORS.length]} radius={index === series.length - 1 ? [4, 4, 0, 0] : undefined} onClick={(entry) => onCellClick?.(xKey, entry?.payload?.[xKey])} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}