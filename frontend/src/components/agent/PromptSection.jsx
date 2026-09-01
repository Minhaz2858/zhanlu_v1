import { useState } from 'react';
import { ScrollText } from 'lucide-react';
import { Section, textareaCls } from './AgentParts';
import { useLanguage } from '@/lib/LanguageProvider';
import { PROMPT_LAYERS, localizedLayer } from '@/lib/agentArchitecture';

export default function PromptSection({ form, update, t }) {
  const { lang } = useLanguage();
  const [active, setActive] = useState(0);
  const layer = localizedLayer(PROMPT_LAYERS[active], lang);

  return (
    <Section title={t.agentConfig.promptEng} desc={t.agentConfig.promptDesc} icon={ScrollText}>
      <div className="mb-4 flex flex-wrap gap-1 border-b border-border pb-2">
        {PROMPT_LAYERS.map((l, i) => {
          const ll = localizedLayer(l, lang);
          return (
            <button
              key={l.key}
              onClick={() => setActive(i)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${active === i ? 'bg-secondary text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
            >
              <span className="mr-1 font-mono text-muted-foreground/60">L{i + 1}</span>
              {ll.label}
            </button>
          );
        })}
      </div>
      <p className="mb-2 text-xs text-muted-foreground">{layer.desc}</p>
      {layer.guide?.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {layer.guide.map((g, i) => (
            <span key={i} className="inline-flex items-center gap-1 rounded-md bg-secondary/60 px-2 py-0.5 text-[11px] text-muted-foreground">
              <span className="h-1 w-1 rounded-full bg-primary/60" /> {g}
            </span>
          ))}
        </div>
      )}
      <textarea
        value={form[layer.key] || ''}
        onChange={(e) => update({ [layer.key]: e.target.value })}
        rows={10}
        className={`${textareaCls} resize-y font-mono text-xs leading-relaxed`}
        placeholder={layer.placeholder}
      />
      <div className="mt-3 flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{t.agentConfig.layer} {active + 1} / {PROMPT_LAYERS.length}</span>
        <span>{(form[layer.key] || '').length} chars</span>
      </div>
    </Section>
  );
}