import { Sparkles } from 'lucide-react';

export default function AgentSuggestions({ agent, onSelect, lang = 'en' }) {
  // Capability pills were auto-rendered next to the selected agent pill and
  // were easily confused with attached skills. Per product feedback, the
  // chat input should show only the agent pill and any skills the user
  // picks manually — no auto-suggested capability chips.
  return null;
  const capabilities = (agent?.capabilities || []).slice(0, 3);
  if (!agent || capabilities.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5 px-3 pb-2">
      {capabilities.map((capability) => (
        <button key={capability} onClick={() => onSelect(lang === 'en' ? `Help me with ${capability}` : `请帮我处理：${capability}`)} className="inline-flex items-center gap-1.5 rounded-full border border-border bg-secondary/40 px-2.5 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground">
          <Sparkles className="h-3 w-3 text-primary" />
          {capability}
        </button>
      ))}
    </div>
  );
}