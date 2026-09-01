import { useState, useEffect } from 'react';
import { Wrench, Factory, ShieldCheck, HardHat, Truck, Gauge, Loader2 } from 'lucide-react';
import { base44 } from '@/api/base44Client';

const CATEGORY_ICONS = {
  production: Factory,
  maintenance: Wrench,
  quality: ShieldCheck,
  safety: HardHat,
  supply: Truck,
  energy: Gauge,
  general: Wrench,
};

export default function RelatedContent({ chips, related, onSelectItem }) {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    base44.entities.Tool.list('-updated_date', 200)
      .then(setTools)
      .catch(() => {})
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  if (!chips || chips.length === 0) return null;
  const lastChip = chips[chips.length - 1];
  const section = related?.[lastChip];
  if (!section) return null;

  const items = tools
    .filter((tool) => tool.category === lastChip)
    .map((tool) => ({ skillObj: tool, text: tool.description }));
  const Icon = CATEGORY_ICONS[lastChip] || Wrench;

  return (
    <div className="mt-3 animate-slide-up rounded-xl border border-border bg-card shadow-sm">
      <div className="px-4 py-3">
        <h4 className="mb-2 text-xs font-medium text-muted-foreground">{section.section}</h4>
        {loading ? (
          <div className="flex items-center gap-2 py-2 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
          </div>
        ) : items.length === 0 ? (
          <p className="py-2 text-xs text-muted-foreground">—</p>
        ) : (
          <div className="space-y-0.5">
            {items.map((item, i) => (
              <button
                key={i}
                onClick={() => onSelectItem(item)}
                className="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left text-sm text-foreground transition-colors hover:bg-secondary/70"
              >
                <Icon className="h-4 w-4 shrink-0 text-primary" />
                <span className="flex-1">{item.text}</span>
                <span className="font-mono text-xs text-muted-foreground">/{item.skillObj.name}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}