import { PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer, Tooltip } from 'recharts';

const COLORS = ['hsl(var(--primary))', 'hsl(var(--chart-2))', 'hsl(var(--chart-3))'];

export default function RadarChartWidget({ widget, rows }) {
  const options = widget.options || {};
  const axisKey = options.axis_column || Object.keys(rows?.[0] || {})[0];
  const series = options.series?.length ? options.series : Object.keys(rows?.[0] || {}).filter((key) => key !== axisKey).map((key) => ({ key, label: key }));
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <RadarChart data={rows || []} outerRadius={82}>
          <PolarGrid />
          <PolarAngleAxis dataKey={axisKey} tick={{ fontSize: 11 }} />
          <PolarRadiusAxis tick={{ fontSize: 10 }} />
          <Tooltip />
          {series.map((item, index) => (
            <Radar key={item.key} name={item.label || item.key} dataKey={item.key} stroke={COLORS[index % COLORS.length]} fill={COLORS[index % COLORS.length]} fillOpacity={0.18} />
          ))}
        </RadarChart>
      </ResponsiveContainer>
    </div>
  );
}