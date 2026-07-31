export function Section({ title, desc, icon: Icon, children }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-4 flex items-start gap-2">
        {Icon && <Icon className="mt-0.5 h-4 w-4 shrink-0 text-primary" />}
        <div>
          <h3 className="font-display text-base text-foreground">{title}</h3>
          {desc && <p className="mt-0.5 text-xs text-muted-foreground">{desc}</p>}
        </div>
      </div>
      {children}
    </div>
  );
}

export function Field({ label, hint, children }) {
  return (
    <div>
      <label className="mb-1.5 block text-xs font-medium text-muted-foreground">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[11px] text-muted-foreground/70">{hint}</p>}
    </div>
  );
}

export function Segmented({ options, value, onChange }) {
  return (
    <div className="inline-flex flex-wrap rounded-lg border border-border bg-secondary/50 p-0.5">
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${value === opt.value ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

export function Slider({ label, value, min, max, step, onChange, format, hint }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        <span className="font-mono text-xs text-foreground">{format ? format(value) : value}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full accent-primary"
      />
      {hint && <p className="mt-1 text-[11px] text-muted-foreground/70">{hint}</p>}
    </div>
  );
}

export function Toggle({ label, desc, checked, onChange }) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className="flex w-full items-center justify-between rounded-lg border border-border bg-background px-3 py-2.5 text-left transition-colors hover:bg-secondary/40"
    >
      <div className="min-w-0">
        <div className="text-sm text-foreground">{label}</div>
        {desc && <div className="text-[11px] text-muted-foreground">{desc}</div>}
      </div>
      <span className={`relative ml-3 h-5 w-9 shrink-0 rounded-full transition-colors ${checked ? 'bg-primary' : 'bg-muted-foreground/30'}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-card shadow transition-transform ${checked ? 'translate-x-4' : 'translate-x-0.5'}`} />
      </span>
    </button>
  );
}

export const inputCls = 'w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/30';
export const textareaCls = 'w-full rounded-lg border border-input bg-background px-3 py-2 text-sm leading-relaxed focus:outline-none focus:ring-2 focus:ring-primary/30';

export const MODELS = [
  { value: 'automatic', label: 'Automatic' },
  { value: 'gpt_5_mini', label: 'GPT-5 Mini' },
  { value: 'gemini_3_flash', label: 'Gemini 3 Flash' },
  { value: 'gpt_5_4', label: 'GPT-5.4' },
  { value: 'gpt_5_5', label: 'GPT-5.5' },
  { value: 'gemini_3_1_pro', label: 'Gemini 3.1 Pro' },
  { value: 'claude_sonnet_4_6', label: 'Claude Sonnet 4.6' },
  { value: 'claude_opus_4_8', label: 'Claude Opus 4.8' },
];