import { Cpu, Network, Wrench, ShieldCheck, Activity, Layers, Gauge, Bot, Brain } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { resolveModelLabel, normalizeTopology } from '@/lib/agentArchitecture';

const PROMPT_LAYERS = ['prompt_identity', 'prompt_boundary', 'prompt_reasoning', 'prompt_tools', 'prompt_output'];

export default function AgentConfigSummary({ agent, t }) {
  const { lang } = useLanguage();
  const modelLabel = resolveModelLabel(agent.model, lang);
  const skills = agent.skills || [];
  const caps = agent.capabilities || [];
  const subAgents = agent.sub_agents || [];
  const promptDone = PROMPT_LAYERS.filter((k) => agent[k] && String(agent[k]).trim()).length;
  const topo = normalizeTopology(agent.topology);

  const metrics = [
    { icon: Cpu, label: t.agentConfig.model, value: modelLabel },
    { icon: Brain, label: t.agentConfig.agentType, value: t.agentConfig.agentTypes?.[agent.agent_type] || 'Sequential' },
    { icon: Network, label: t.agentConfig.topology, value: `${t.agentConfig.topologies[topo]}${subAgents.length ? ` · ${subAgents.length}` : ''}` },
    { icon: Wrench, label: t.agentConfig.skills, value: `${skills.length} ${t.agentConfig.items}` },
    { icon: ShieldCheck, label: t.agentConfig.capabilities, value: `${caps.length} ${t.agentConfig.items}` },
    { icon: Layers, label: t.agentConfig.promptEng, value: `${promptDone}/5` },
    { icon: Activity, label: t.agentConfig.trace, value: agent.trace_enabled ? t.agentConfig.enabled : t.agentConfig.disabled },
  ];

  const controlBits = [
    { on: agent.data_read, label: t.agentConfig.dataRead },
    { on: agent.data_write, label: t.agentConfig.dataWrite },
    { on: agent.human_fallback, label: t.agentConfig.humanFallback },
  ].filter((b) => b.on);

  return (
    <div className="mt-3 space-y-3 border-t border-border pt-3">
      <div className="grid grid-cols-2 gap-x-3 gap-y-2">
        {metrics.map((m) => (
          <div key={m.label} className="flex items-start gap-1.5 text-[11px]">
            <m.icon className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
            <div className="min-w-0">
              <div className="text-muted-foreground">{m.label}</div>
              <div className="truncate font-medium text-foreground">{m.value}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {controlBits.map((b) => (
          <span key={b.label} className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[10px] text-foreground">
            <Gauge className="h-2.5 w-2.5" /> {b.label}
          </span>
        ))}
        <span className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[10px] text-foreground">
          <Bot className="h-2.5 w-2.5" /> {t.agentConfig.maxCalls} {agent.max_call_count ?? 50}
        </span>
        {agent.trace_enabled && (
          <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
            <Activity className="h-2.5 w-2.5" /> {String(agent.log_level || 'info').toUpperCase()}
          </span>
        )}
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
          <span>{t.agentConfig.completeness}</span>
          <span className="font-mono text-foreground">{calcPct(agent)}%</span>
        </div>
        <div className="h-1 w-full overflow-hidden rounded-full bg-secondary">
          <div
            className={`h-full rounded-full ${calcPct(agent) === 100 ? 'bg-primary' : 'bg-accent-foreground/40'}`}
            style={{ width: `${calcPct(agent)}%` }}
          />
        </div>
      </div>
    </div>
  );
}

function calcPct(a) {
  const checks = [
    !!a.name, !!(a.description && String(a.description).trim()),
    PROMPT_LAYERS.some((k) => a[k] && String(a[k]).trim()),
    (a.skills || []).length > 0,
    (a.capabilities || []).length > 0,
    a.topology && a.topology !== 'standalone',
    a.trace_enabled,
    !!(a.model && a.model !== 'automatic'),
  ];
  return Math.round(checks.filter(Boolean).length / checks.length * 100);
}