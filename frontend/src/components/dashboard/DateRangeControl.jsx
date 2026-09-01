// Date-range selector for the dashboard viewer. Presets resolve to ISO from/to
// datetimes; "custom" reveals two date inputs. The selected window is threaded
// into widget SQL via :from/:to/:date tokens on the backend.

export function presetToRange(preset) {
  const to = new Date();
  let from = new Date();
  if (preset === '7d') from.setDate(to.getDate() - 7);
  else if (preset === '30d') from.setDate(to.getDate() - 30);
  else if (preset === '90d') from.setDate(to.getDate() - 90);
  else if (preset === 'ytd') from = new Date(to.getFullYear(), 0, 1);
  return { preset, from: from.toISOString(), to: to.toISOString() };
}

export default function DateRangeControl({ value, onChange }) {
  return (
    <div className="flex items-center gap-2">
      <select
        aria-label="Date range"
        value={value.preset}
        onChange={(e) =>
          onChange(e.target.value === 'custom' ? { ...value, preset: 'custom' } : presetToRange(e.target.value))
        }
        className="rounded-md border border-border bg-background px-2 py-1 text-xs"
      >
        <option value="7d">Last 7 days</option>
        <option value="30d">Last 30 days</option>
        <option value="90d">Last 90 days</option>
        <option value="ytd">Year to date</option>
        <option value="custom">Custom</option>
      </select>
      {value.preset === 'custom' && (
        <>
          <input
            type="date"
            aria-label="From date"
            value={value.from ? value.from.slice(0, 10) : ''}
            onChange={(e) => onChange({ ...value, from: new Date(e.target.value).toISOString() })}
            className="rounded-md border border-border bg-background px-2 py-1 text-xs"
          />
          <input
            type="date"
            aria-label="To date"
            value={value.to ? value.to.slice(0, 10) : ''}
            onChange={(e) => onChange({ ...value, to: new Date(e.target.value).toISOString() })}
            className="rounded-md border border-border bg-background px-2 py-1 text-xs"
          />
        </>
      )}
    </div>
  );
}
