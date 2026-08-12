import { CartesianGrid, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis } from 'recharts';
import { formatAxisTick } from '../format';

export default function ScatterChartWidget({ widget, rows, onCellClick }) {
  const options = widget.options || {};
  const xKey = options.x_column || Object.keys(rows?.[0] || {})[0];
  const yKey = options.y_column || Object.keys(rows?.[0] || {})[1];
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey={xKey} type="number" tickFormatter={formatAxisTick} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis dataKey={yKey} type="number" tickFormatter={formatAxisTick} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={48} />
          <Tooltip />
          <Scatter data={rows || []} fill="hsl(var(--primary))" onClick={(entry) => onCellClick?.(xKey, entry?.payload?.[xKey])} />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}