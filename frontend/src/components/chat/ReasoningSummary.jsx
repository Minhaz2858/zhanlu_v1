import { useState } from 'react';
import { Activity, CheckCircle2, ChevronDown, ChevronRight } from 'lucide-react';

export default function ReasoningSummary({ trace = [], lang = 'en', onOpen }) {
  const [expanded, setExpanded] = useState(false);
  if (!trace.length) return null;
  const completed = trace.filter((step) => step.status !== 'failed').length;
  return (
    <div className="mt-3 overflow-hidden rounded-lg border border-border bg-secondary/30">
      <button onClick={() => setExpanded(!expanded)} className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-muted-foreground hover:text-foreground">
        <Activity className="h-3.5 w-3.5 text-primary" />
        <span className="font-medium">{lang === 'en' ? 'Reasoning & actions' : '推理与执行'}</span>
        <span className="ml-auto">{completed}/{trace.length}</span>
        {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
      </button>
      {expanded && (
        <div className="space-y-2 border-t border-border px-3 py-2">
          {trace.map((step, index) => (
            <div key={index} className="flex gap-2 text-xs">
              <CheckCircle2 className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${step.status === 'failed' ? 'text-destructive' : 'text-primary'}`} />
              <div><div className="font-medium text-foreground">{step.title}</div>{step.detail && <div className="mt-0.5 text-muted-foreground">{step.detail}</div>}</div>
            </div>
          ))}
          {onOpen && <button onClick={onOpen} className="text-xs font-medium text-primary hover:underline">{lang === 'en' ? 'Open full trace' : '打开完整追踪'}</button>}
        </div>
      )}
    </div>
  );
}