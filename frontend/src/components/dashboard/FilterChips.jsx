import { X } from 'lucide-react';

// Active cross-widget filter chips. Each chip clears its dimension on click.
export default function FilterChips({ filters, onClear }) {
  const entries = Object.entries(filters || {});
  if (!entries.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {entries.map(([token, value]) => (
        <button
          key={token}
          onClick={() => onClear(token)}
          className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium text-secondary-foreground transition-colors hover:bg-secondary/70"
        >
          {token}: {value}
          <X className="h-3 w-3" />
        </button>
      ))}
    </div>
  );
}
