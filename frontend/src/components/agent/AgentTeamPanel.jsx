import { useState } from 'react';
import { Cpu, Layers, ChevronRight, Users, Box, Workflow, ArrowLeft } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { TEAM_TOPOLOGIES, localizedTopology, localizedAgentType, buildAdkSpec } from '@/lib/agentArchitecture';
import TopologyDiagram from './TopologyDiagram';

export default function AgentTeamPanel({ form, updateRoot, t }) {
  const { lang } = useLanguage();
  const [showSpec, setShowSpec] = useState(false);

  const aType = localizedAgentType(form.agent_type, lang);
  const isTeam = form.topology && form.topology !== 'standalone';
  const subs = form.sub_agents || [];
  const flowMode = !!form.flow_mode;
  const spec = buildAdkSpec(form, lang);

  function setMode(team) {
    updateRoot({ topology: team ? 'sequence' : 'standalone', sub_agents: team ? subs : [] });
  }

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-4 flex items-center justify-between">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t.agentConfig.orchestration}</p>
        <span className="inline-flex items-center gap-1 rounded-md bg-secondary/60 px-2 py-0.5 text-[11px] text-muted-foreground">
          <Cpu className="h-3 w-3 text-primary" /> {aType.label}
        </span>
      </div>

      {!flowMode ? (
        <>
          {/* 简洁模式：单一 / 团队 */}
          <div className="mb-4 grid grid-cols-2 gap-2">
            <button
              onClick={() => setMode(false)}
              className={`flex items-center gap-2 rounded-lg border px-3 py-2.5 text-left transition-colors ${!isTeam ? 'border-primary bg-primary/5' : 'border-border hover:bg-secondary/40'}`}
            >
              <Box className={`h-4 w-4 ${!isTeam ? 'text-primary' : 'text-muted-foreground'}`} />
              <div>
                <div className="text-xs font-medium text-foreground">{lang === 'en' ? 'Single Agent' : '单一智能体'}</div>
                <div className="text-[10px] text-muted-foreground">{lang === 'en' ? 'No sub-agents' : '无子代理'}</div>
              </div>
            </button>
            <button
              onClick={() => setMode(true)}
              className={`flex items-center gap-2 rounded-lg border px-3 py-2.5 text-left transition-colors ${isTeam ? 'border-primary bg-primary/5' : 'border-border hover:bg-secondary/40'}`}
            >
              <Users className={`h-4 w-4 ${isTeam ? 'text-primary' : 'text-muted-foreground'}`} />
              <div>
                <div className="text-xs font-medium text-foreground">{lang === 'en' ? 'Agent Team' : '多智能体团队'}</div>
                <div className="text-[10px] text-muted-foreground">{lang === 'en' ? 'Root + Sub agents' : '根代理 + 子代理'}</div>
              </div>
            </button>
          </div>

          {/* 团队拓扑三选一 */}
          {isTeam && (
            <div className="mb-4">
              <label className="mb-2 block text-xs font-medium text-muted-foreground">{t.agentConfig.topology}</label>
              <div className="grid grid-cols-3 gap-2">
                {TEAM_TOPOLOGIES.map((tp) => {
                  const lt = localizedTopology(tp.value, lang);
                  return (
                    <button
                      key={tp.value}
                      onClick={() => updateRoot({ topology: tp.value })}
                      className={`flex h-full flex-col items-center gap-1.5 rounded-lg border px-1.5 py-2 text-center transition-colors ${form.topology === tp.value ? 'border-primary bg-primary/5' : 'border-border hover:bg-secondary/40'}`}
                    >
                      <TopologyDiagram topology={tp.value} />
                      <div className={`text-[11px] font-medium ${form.topology === tp.value ? 'text-primary' : 'text-foreground'}`}>{lt.label}</div>
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </>
      ) : (
        /* 高级编排模式提示 */
        <div className="mb-4 rounded-lg border border-primary/30 bg-primary/5 p-3">
          <div className="flex items-center gap-1.5 text-xs font-medium text-primary">
            <Workflow className="h-3.5 w-3.5" /> {t.agentConfig.flowMode}
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{t.agentConfig.flowModeDesc}</p>
        </div>
      )}

      {/* 简洁 / 高级 双模式切换 */}
      <button
        onClick={() => updateRoot({ flow_mode: !flowMode })}
        className="flex w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
      >
        {flowMode ? (
          <><ArrowLeft className="h-3 w-3" /> {t.agentConfig.backToSimple}</>
        ) : (
          <><Workflow className="h-3 w-3" /> {t.agentConfig.enterAdvanced}</>
        )}
      </button>

      {/* ADK 配置预览 */}
      <button
        onClick={() => setShowSpec(!showSpec)}
        className="mt-3 flex w-full items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        <Layers className="h-3 w-3" /> {t.agentConfig.adkSpec}
        <ChevronRight className={`ml-auto h-3 w-3 transition-transform ${showSpec ? 'rotate-90' : ''}`} />
      </button>
      {showSpec && (
        <pre className="mt-2 overflow-x-auto rounded-md bg-secondary/40 p-3 font-mono text-[11px] leading-relaxed text-foreground">{spec}</pre>
      )}
    </div>
  );
}