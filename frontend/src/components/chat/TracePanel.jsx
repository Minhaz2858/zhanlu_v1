import { X, Brain, Wrench, FileOutput, CheckCircle2, XCircle, MinusCircle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

const TYPE_META = {
  thinking: { icon: Brain, label: { zh: '推理思考', en: 'Thinking' }, color: 'text-chart-3', ring: 'bg-chart-3/10' },
  tool: { icon: Wrench, label: { zh: '工具调用', en: 'Tool Call' }, color: 'text-chart-2', ring: 'bg-chart-2/10' },
  output: { icon: FileOutput, label: { zh: '输出组装', en: 'Output' }, color: 'text-primary', ring: 'bg-primary/10' },
};

const STATUS_META = {
  success: { icon: CheckCircle2, label: { zh: '成功', en: 'Success' }, color: 'text-chart-2' },
  failed: { icon: XCircle, label: { zh: '失败', en: 'Failed' }, color: 'text-destructive' },
  skipped: { icon: MinusCircle, label: { zh: '跳过', en: 'Skipped' }, color: 'text-muted-foreground' },
};

export default function TracePanel({ message, onClose }) {
  const { lang } = useLanguage();
  const isEn = lang === 'en';
  const trace = Array.isArray(message?.trace) ? message.trace : [];
  const totalMs = trace.reduce((s, t) => s + (Number(t.duration_ms) || 0), 0);

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/20 animate-fade-in" onClick={onClose} />
      <div className="relative flex h-full w-full max-w-md flex-col border-l border-border bg-card shadow-xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <h2 className="font-display text-base text-foreground">{isEn ? 'Execution Trace' : '执行追踪'}</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {trace.length} {isEn ? 'steps' : '步'} · {totalMs}ms
            </p>
          </div>
          <button onClick={onClose} className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4">
          {trace.length === 0 ? (
            <p className="mt-8 text-center text-sm text-muted-foreground">{isEn ? 'No trace recorded.' : '该消息未记录追踪。'}</p>
          ) : (
            <ol className="relative space-y-4 before:absolute before:left-[11px] before:top-2 before:bottom-2 before:w-px before:bg-border">
              {trace.map((t, i) => {
                const meta = TYPE_META[t.type] || TYPE_META.thinking;
                const sm = STATUS_META[t.status] || STATUS_META.success;
                const Icon = meta.icon;
                const SIcon = sm.icon;
                return (
                  <li key={t.id || `trace-${t.step || i}`} className="relative flex gap-3">
                    <div className={`relative z-10 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border ${meta.ring} ${meta.color}`}>
                      <Icon className="h-3 w-3" />
                    </div>
                    <div className="flex-1 pb-1">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-medium text-muted-foreground">#{t.step || i + 1}</span>
                        <span className="text-sm font-medium text-foreground">{t.title || (isEn ? 'Untitled step' : '未命名步骤')}</span>
                        <span className={`ml-auto inline-flex items-center gap-1 text-[10px] ${sm.color}`}>
                          <SIcon className="h-3 w-3" /> {sm.label[isEn ? 'en' : 'zh']}
                        </span>
                      </div>
                      <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                        <span className={`inline-flex items-center gap-1 ${meta.color}`}><Icon className="h-2.5 w-2.5" />{meta.label[isEn ? 'en' : 'zh']}</span>
                        {t.duration_ms != null && <span>· {t.duration_ms}ms</span>}
                      </div>
                      {t.detail && (
                        <div className="mt-2 whitespace-pre-wrap rounded-lg bg-secondary/50 px-3 py-2 text-xs leading-relaxed text-foreground/80">
                          {t.detail}
                        </div>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      </div>
    </div>
  );
}