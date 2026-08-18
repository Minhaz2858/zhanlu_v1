import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  PolarAngleAxis,
  PolarGrid,
  Radar,
  RadarChart,
  RadialBar,
  RadialBarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

const PALETTE = ['#2563eb', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16'];

/** Convert columnar SQL rows [[v1,v2],[v1,v2]] into objects keyed by column name. */
export function rowsToObjects(columns, rows) {
  if (!Array.isArray(columns) || !Array.isArray(rows)) return [];
  return rows
    .filter((r) => Array.isArray(r))
    .map((r) => {
      const obj = {};
      columns.forEach((c, i) => {
        obj[c] = r[i];
      });
      return obj;
    });
}

function numericKeys(objects, xKey) {
  if (!objects.length) return [];
  const first = objects[0];
  return Object.keys(first).filter((k) => k !== xKey && typeof Number(first[k]) === 'number' && first[k] !== null && first[k] !== '');
}

function tooltipStyle() {
  return {
    contentStyle: {
      background: 'var(--tw-bg-opacity, 1)',
      backgroundColor: '#fff',
      border: '1px solid #e2e8f0',
      borderRadius: '0.5rem',
      fontSize: '12px',
    },
  };
}

function Card({ title, children, className = '', status }) {
  return (
    <section className={`flex flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 ${className}`}>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="truncate text-sm font-semibold text-slate-700 dark:text-slate-200">{title}</h2>
        {status ? (
          <span className="ml-2 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" title="live" />
        ) : null}
      </div>
      <div className="min-h-0 flex-1">{children}</div>
    </section>
  );
}

function Empty({ error }) {
  return (
    <div className="flex h-40 items-center justify-center text-xs text-slate-400 dark:text-slate-500">
      {error || 'No data'}
    </div>
  );
}

function KpiWidget({ objects, options }) {
  if (!objects.length) return <Empty />;
  const first = objects[0];
  const keys = numericKeys(objects, '');
  if (!keys.length) return <Empty />;
  const key = keys[keys.length - 1];
  const value = first[key];
  const num = Number(value);
  const formatted = Number.isFinite(num)
    ? num.toLocaleString(undefined, { maximumFractionDigits: 2 })
    : String(value ?? '');
  const unit = options?.unit || '';
  const color = options?.color || '#2563eb';
  return (
    <div className="flex h-40 flex-col justify-center">
      <div className="text-3xl font-bold tracking-tight" style={{ color }}>
        {formatted}
        {unit ? <span className="ml-1 text-base font-medium text-slate-400">{unit}</span> : null}
      </div>
      <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{key}</div>
    </div>
  );
}

function LineWidget({ objects, type, options }) {
  if (!objects.length) return <Empty />;
  const xKey = Object.keys(objects[0])[0];
  const keys = numericKeys(objects, xKey);
  if (!keys.length) return <Empty />;
  const Chart = type === 'area' ? AreaChart : LineChart;
  const Series = type === 'area' ? Area : Line;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <Chart data={objects} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis dataKey={xKey} tick={{ fontSize: 11 }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
        <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={52} />
        <Tooltip {...tooltipStyle()} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {keys.map((k, i) =>
          type === 'area' ? (
            <Area
              key={k}
              type="monotone"
              dataKey={k}
              stroke={options?.colors?.[i] || PALETTE[i % PALETTE.length]}
              fill={options?.colors?.[i] || PALETTE[i % PALETTE.length]}
              fillOpacity={0.12}
              strokeWidth={2}
            />
          ) : (
            <Line
              key={k}
              type="monotone"
              dataKey={k}
              stroke={options?.colors?.[i] || PALETTE[i % PALETTE.length]}
              strokeWidth={2}
              dot={false}
            />
          ),
        )}
      </Chart>
    </ResponsiveContainer>
  );
}

function BarWidget({ objects, options }) {
  if (!objects.length) return <Empty />;
  const xKey = Object.keys(objects[0])[0];
  const keys = numericKeys(objects, xKey);
  if (!keys.length) return <Empty />;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={objects} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
        <XAxis dataKey={xKey} tick={{ fontSize: 11 }} tickLine={false} axisLine={{ stroke: '#cbd5e1' }} />
        <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={52} />
        <Tooltip {...tooltipStyle()} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {keys.map((k, i) => (
          <Bar key={k} dataKey={k} fill={options?.colors?.[i] || PALETTE[i % PALETTE.length]} radius={[4, 4, 0, 0]} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

function PieWidget({ objects }) {
  if (!objects.length) return <Empty />;
  const labelKey = Object.keys(objects[0])[0];
  const valueKey = numericKeys(objects, labelKey)[0] || Object.keys(objects[0])[1];
  if (!valueKey) return <Empty />;
  const data = objects.map((o, i) => ({
    name: String(o[labelKey] ?? `#${i + 1}`),
    value: Number(o[valueKey]) || 0,
  }));
  return (
    <ResponsiveContainer width="100%" height={220}>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" innerRadius={45} outerRadius={80} paddingAngle={2}>
          {data.map((_, i) => (
            <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
          ))}
        </Pie>
        <Tooltip {...tooltipStyle()} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
      </PieChart>
    </ResponsiveContainer>
  );
}

function TableWidget({ columns, rows }) {
  if (!rows || !rows.length) return <Empty />;
  return (
    <div className="max-h-64 overflow-auto rounded-lg border border-slate-100 dark:border-slate-800">
      <table className="w-full text-left text-xs">
        <thead className="sticky top-0 bg-slate-50 dark:bg-slate-800">
          <tr>
            {columns.map((c) => (
              <th key={c} className="px-3 py-2 font-semibold text-slate-500 dark:text-slate-300">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-slate-100 dark:border-slate-800">
              {columns.map((c, j) => (
                <td key={c} className="px-3 py-1.5 text-slate-700 dark:text-slate-300">
                  {r[j] == null ? '' : String(r[j])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function GaugeWidget({ objects, options }) {
  if (!objects.length) return <Empty />;
  const keys = numericKeys(objects, '');
  if (!keys.length) return <Empty />;
  const value = Number(objects[0][keys[0]]) || 0;
  const max = Number(options?.max) || 100;
  const color = options?.color || PALETTE[0];
  const data = [{ name: keys[0], value: Math.max(0, Math.min(value, max)) }];
  return (
    <div className="flex h-[220px] flex-col items-center">
      <ResponsiveContainer width="100%" height={170}>
        <RadialBarChart data={data} innerRadius="70%" outerRadius="100%" startAngle={210} endAngle={-30}>
          <PolarAngleAxis type="number" domain={[0, max]} angleAxisId={0} tick={false} />
          <RadialBar dataKey="value" fill={color} background={{ fill: '#e2e8f0' }} cornerRadius={8} />
        </RadialBarChart>
      </ResponsiveContainer>
      <div className="-mt-10 text-xl font-bold" style={{ color }}>
        {value.toLocaleString(undefined, { maximumFractionDigits: 1 })}
        <span className="ml-1 text-xs font-medium text-slate-400">/ {max}</span>
      </div>
    </div>
  );
}

function RadarWidget({ objects }) {
  if (!objects.length) return <Empty />;
  const xKey = Object.keys(objects[0])[0];
  const keys = numericKeys(objects, xKey);
  if (!keys.length) return <Empty />;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <RadarChart data={objects}>
        <PolarGrid stroke="#e2e8f0" />
        <PolarAngleAxis dataKey={xKey} tick={{ fontSize: 10 }} />
        {keys.map((k, i) => (
          <Radar
            key={k}
            name={k}
            dataKey={k}
            stroke={PALETTE[i % PALETTE.length]}
            fill={PALETTE[i % PALETTE.length]}
            fillOpacity={0.18}
          />
        ))}
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Tooltip {...tooltipStyle()} />
      </RadarChart>
    </ResponsiveContainer>
  );
}

/** Renders one metric (any widget type) inside a Card. */
export function Widget({ metric, data, className = '' }) {
  const error = data?.error;
  const columns = data?.columns || [];
  const rows = data?.rows || [];
  const objects = rowsToObjects(columns, rows);
  const options = metric.options || {};

  let body;
  switch (metric.type) {
    case 'kpi':
      body = <KpiWidget objects={objects} options={options} />;
      break;
    case 'line':
    case 'area':
      body = <LineWidget objects={objects} type={metric.type} options={options} />;
      break;
    case 'bar':
      body = <BarWidget objects={objects} options={options} />;
      break;
    case 'pie':
      body = <PieWidget objects={objects} />;
      break;
    case 'table':
      body = <TableWidget columns={columns} rows={rows} />;
      break;
    case 'gauge':
      body = <GaugeWidget objects={objects} options={options} />;
      break;
    case 'radar':
      body = <RadarWidget objects={objects} />;
      break;
    default:
      body = <Empty error={`Unknown widget type: ${metric.type}`} />;
  }

  return (
    <Card title={metric.title} className={className} status={!error && objects.length > 0}>
      {error ? <Empty error={error} /> : body}
    </Card>
  );
}
