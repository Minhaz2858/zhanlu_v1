import { useMemo } from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from 'recharts';
import { getChartTheme, rechartsTooltipStyle } from '../../lib/chartTheme';

export default function PriceTrendChart({ data, title, height = 200 }) {
  const theme = getChartTheme();

  const chartData = useMemo(() => {
    if (!data || !Array.isArray(data)) return [];
    return data.map((d) => ({
      date: d.date?.slice(5) || d.date, // "MM-DD" for display
      fullDate: d.date,
      price: d.price,
    }));
  }, [data]);

  if (!chartData.length) {
    return (
      <div className="flex items-center justify-center text-xs text-slate-400 h-[200px]">
        No price data available
      </div>
    );
  }

  const prices = chartData.map((d) => d.price).filter((p) => p != null);
  const minP = Math.min(...prices);
  const maxP = Math.max(...prices);
  const avgP = prices.reduce((a, b) => a + b, 0) / prices.length;

  return (
    <div>
      <h4 className="text-xs font-semibold text-slate-600 mb-1">{title}</h4>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={chartData} margin={{ top: 5, right: 10, left: 5, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: '#94a3b8' }}
            interval="preserveStartEnd"
          />
          <YAxis
            domain={[Math.floor(minP * 0.98), Math.ceil(maxP * 1.02)]}
            tick={{ fontSize: 10, fill: '#94a3b8' }}
            tickFormatter={(v) => v.toLocaleString()}
          />
          <Tooltip
            {...rechartsTooltipStyle(theme)}
            formatter={(value) => [`${value?.toLocaleString()} 元/吨`, 'Price']}
            labelFormatter={(label, payload) => payload?.[0]?.payload?.fullDate || label}
          />
          <ReferenceLine y={avgP} stroke="#94a3b8" strokeDasharray="4 4" />
          <Line
            type="monotone"
            dataKey="price"
            stroke="#3B82F6"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, fill: '#3B82F6' }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
