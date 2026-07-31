import { CheckCircle2, Circle } from 'lucide-react';

const NAVIGABLE = new Set(['role', 'prompt', 'skills', 'data', 'caps']);

const MODULES = [
  { key: 'role', fields: ['name', 'description'] },
  { key: 'prompt', fields: ['prompt_identity', 'prompt_boundary', 'prompt_reasoning', 'prompt_tools', 'prompt_output'] },
  { key: 'skills', fields: ['skills'] },
  { key: 'collab', fields: ['topology'] },
  { key: 'control', fields: ['max_call_count', 'max_retries'] },
  { key: 'trace', fields: ['trace_enabled'] },
  { key: 'model', fields: ['model'] },
  { key: 'caps', fields: ['capabilities'] },
  { key: 'data', fields: ['knowledge_bases'] },
];

export default function ConfigOverview({ form, t, onNavigate }) {
  const checklist = MODULES.map((m) => {
    const done = m.fields.every((f) => {
      const v = form[f];
      if (Array.isArray(v)) return v.length > 0;
      if (typeof v === 'boolean') return true;
      return v && String(v).trim() !== '';
    });
    return { key: m.key, label: t.agentConfig.overview?.[m.key] || m.key, done };
  });
  const completed = checklist.filter((c) => c.done).length;
  const pct = Math.round((completed / checklist.length) * 100);

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t.agentConfig.completeness}</p>
        <span className={`font-mono text-sm ${pct === 100 ? 'text-primary' : 'text-foreground'}`}>{pct}%</span>
      </div>
      <div className="mb-4 h-1.5 w-full overflow-hidden rounded-full bg-secondary">
        <div
          className={`h-full rounded-full transition-all duration-500 ${pct === 100 ? 'bg-primary' : 'bg-accent-foreground/40'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="space-y-1.5">
        {checklist.map((c) => {
          const navigable = NAVIGABLE.has(c.key) && onNavigate;
          const content = (
            <>
              {c.done ? (
                <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-primary" />
              ) : (
                <Circle className="h-3.5 w-3.5 shrink-0 text-muted-foreground/40" />
              )}
              <span className={c.done ? 'text-foreground' : 'text-muted-foreground'}>{c.label}</span>
            </>
          );
          return navigable ? (
            <button key={c.key} type="button" onClick={() => onNavigate(c.key)} className="flex w-full items-center gap-2 rounded-md px-1 py-0.5 text-left text-xs transition-colors hover:bg-secondary">
              {content}
            </button>
          ) : (
            <div key={c.key} className="flex items-center gap-2 px-1 py-0.5 text-xs">{content}</div>
          );
        })}
      </div>
    </div>
  );
}