import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { formatAxisTick } from '../format';

export default function AreaChartWidget({ widget, rows, onCellClick }) {
  const options = widget.options || {};
  const xKey = options.x_column || Object.keys(rows?.[0] || {})[0];
  const yKey = options.y_column || Object.keys(rows?.[0] || {})[1];
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={rows || []} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey={xKey} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tickFormatter={formatAxisTick} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={48} />
          <Tooltip />
          <Area type="monotone" dataKey={yKey} stroke="hsl(var(--primary))" fill="hsl(var(--primary))" fillOpacity={0.18} strokeWidth={2} onClick={(entry) => onCellClick?.(xKey, entry?.payload?.[xKey])} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}