import { ArrowRight, Database, Network, Sparkles, Target } from 'lucide-react';

/* The core principle of the Enterprise Ontology, as one glance:
   Enterprise Data → Business Ontology → AI Intelligence → Business Decisions.
   Layers are clickable; captions stay business-language. */

const LAYERS = [
  { key: 'data', label: 'Business Data', caption: 'Databases & knowledge bases',
    icon: Database },
  { key: 'ontology', label: 'Enterprise Ontology', caption: 'Objects, relationships & rules',
    icon: Network },
  { key: 'ai', label: 'AI Intelligence', caption: 'Governed assistants at work',
    icon: Sparkles },
  { key: 'decisions', label: 'Business Decisions', caption: 'Human-approved outcomes',
    icon: Target },
];

export default function ArchitectureFlow({ activeLayer, onLayerClick }) {
  return (
    <div className="flex flex-wrap items-stretch gap-0" aria-label="How the enterprise ontology works">
      {LAYERS.map((l, i) => {
        const Icon = l.icon;
        const active = activeLayer === l.key;
        return (
          <div key={l.key} className="flex items-center">
            <button onClick={() => onLayerClick?.(l.key)}
              aria-pressed={active}
              className={`flex items-center gap-3 rounded-xl border px-5 py-3 text-left transition-colors ${
                active
                  ? 'border-emerald-300 bg-emerald-50/60'
                  : 'border-stone-200 bg-white hover:border-stone-300'}`}>
              <Icon size={18} className={active ? 'text-emerald-600' : 'text-stone-400'} />
              <span>
                <span className="block text-[13px] font-medium text-stone-800">{l.label}</span>
                <span className="block text-[11px] text-stone-500">{l.caption}</span>
              </span>
            </button>
            {i < LAYERS.length - 1 && (
              <ArrowRight size={16} className="mx-2 shrink-0 text-stone-300" aria-hidden="true" />
            )}
          </div>
        );
      })}
    </div>
  );
}
