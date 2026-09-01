import { useState, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { Brain, Bot, Gauge } from 'lucide-react';

export default function StrategicOverview() {
  const { t } = useLanguage();
  const [data, setData] = useState(null);

  useEffect(() => { load(); }, []);
  async function load() {
    const [decisions, tasks, agents] = await Promise.all([
      base44.entities.DecisionFlow.list('', 1).then((r) => r.length).catch(() => 0),
      base44.entities.AutomationTask.list('-updated_date', 200).catch(() => []),
      base44.entities.AgentApp.list('', 200).catch(() => []),
    ]);
    const activeAgents = agents.filter((a) => a.status === 'active').length;
    const ok = tasks.filter((x) => x.status === 'completed' || x.status === 'running').length;
    const successRate = tasks.length > 0 ? Math.round((ok / tasks.length) * 100) : 100;
    setData({ decisions, activeAgents, successRate });
  }

  if (!data) return null;

  const kpis = [
    { label: t.strategic.decisions, value: data.decisions, icon: Brain },
    { label: t.strategic.activeAgents, value: data.activeAgents, icon: Bot },
    { label: t.strategic.successRate, value: `${data.successRate}%`, icon: Gauge },
  ];

  return (
    <div className="shrink-0 border-b border-border bg-sidebar/50">
      <div className="flex items-center gap-7 px-6 py-2.5">
        {kpis.map((k) => (
          <div key={k.label} className="flex items-center gap-2">
            <k.icon className="h-4 w-4 text-muted-foreground" />
            <div>
              <div className="font-display text-lg leading-none text-foreground">{k.value}</div>
              <div className="mt-0.5 text-[10px] text-muted-foreground">{k.label}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}