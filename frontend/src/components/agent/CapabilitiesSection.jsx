import { useState } from 'react';
import { Plus, X } from 'lucide-react';
import { Section } from './AgentParts';

// Defensive: an earlier version of the Step 1 form saved capabilities
// as a comma-separated string into a JSON column. Row reads can
// therefore hand us either an array or a bare string. Crash-proof by
// coercing here so a string never reaches .map()/.filter().
function coerceCaps(value) {
  if (Array.isArray(value)) return value.map((s) => String(s).trim()).filter(Boolean);
  if (typeof value !== 'string') return [];
  return value
    .split(/[,，;；、/]/g)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function CapabilitiesSection({ form, update, t }) {
  const [capInput, setCapInput] = useState('');
  const caps = coerceCaps(form?.capabilities);

  function addCap() {
    const v = capInput.trim();
    if (v && !caps.includes(v)) {
      update({ capabilities: [...caps, v] });
    }
    setCapInput('');
  }

  return (
    <Section title={t.agentConfig.capabilities}>
      <div className="flex flex-wrap gap-2">
        {caps.map((c) => (
          <span key={c} className="inline-flex items-center gap-1 rounded-md bg-secondary px-2.5 py-1 text-xs text-foreground">
            {c}
            <button
              onClick={() => update({ capabilities: caps.filter((x) => x !== c) })}
              className="text-muted-foreground hover:text-destructive"
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
        <div className="flex items-center gap-1">
          <input
            value={capInput}
            onChange={(e) => setCapInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addCap(); } }}
            placeholder={t.agentConfig.capPh}
            className="w-40 rounded-md border border-input bg-background px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-primary/30"
          />
          <button onClick={addCap} className="rounded-md border border-border p-1 text-muted-foreground hover:text-foreground">
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </Section>
  );
}