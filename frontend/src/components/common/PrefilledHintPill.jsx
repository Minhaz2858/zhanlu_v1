import { X } from 'lucide-react';

export default function PrefilledHintPill({ label, onDismiss }) {
  if (!label) return null;
  return (
    <div className="flex items-center gap-1.5 px-3 pt-2">
      <span className="inline-flex items-center gap-1 rounded-md bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
        {label}
        <button
          type="button"
          onClick={() => onDismiss && onDismiss()}
          className="text-primary/60 hover:text-primary"
          aria-label="Dismiss suggestion"
        >
          <X className="h-3 w-3" />
        </button>
      </span>
    </div>
  );
}
