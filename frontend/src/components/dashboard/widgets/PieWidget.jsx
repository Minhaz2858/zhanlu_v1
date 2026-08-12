import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';

const COLORS = ['hsl(var(--primary))', 'hsl(var(--chart-2))', 'hsl(var(--chart-3))', 'hsl(var(--chart-4))', 'hsl(var(--chart-5))'];

export default function PieWidget({ widget, rows, onCellClick }) {
  const options = widget.options || {};
  const nameKey = options.name_column || Object.keys(rows?.[0] || {})[0];
  const valueKey = options.value_column || Object.keys(rows?.[0] || {})[1];
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie data={rows || []} dataKey={valueKey} nameKey={nameKey} innerRadius={48} outerRadius={82} paddingAngle={2} onClick={(entry) => onCellClick?.(nameKey, entry?.[nameKey])}>
            {(rows || []).map((_row, index) => <Cell key={index} fill={COLORS[index % COLORS.length]} />)}
          </Pie>
          <Tooltip />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}