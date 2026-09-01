import { Activity, BarChart3, CheckCircle2, LayoutDashboard, TrendingUp } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

const icons = [BarChart3, TrendingUp, Activity, CheckCircle2];

export default function DashboardArtifactPreview({ title, summary, status }) {
  const { lang } = useLanguage();
  const items = String(summary || '').split('\n').map((line) => line.replace(/^\s*[-*•]\s*/, '').trim()).filter((line) => line && !line.startsWith('#')).slice(0, 8);
  return (
    <div className="min-h-[560px] rounded-lg border border-border bg-background p-5">
      <div className="flex items-start justify-between gap-4 border-b border-border pb-4">
        <div><div className="mb-1 flex items-center gap-2 text-xs font-medium text-primary"><LayoutDashboard className="h-4 w-4" />{lang === 'en' ? 'Dashboard Preview' : '仪表盘预览'}</div><h2 className="font-heading text-xl font-semibold text-foreground">{title}</h2></div>
        <span className="rounded-full bg-secondary px-2.5 py-1 text-xs text-muted-foreground">{status || (lang === 'en' ? 'Ready' : '就绪')}</span>
      </div>
      {items.length > 0 ? (
        <div className="mt-5 grid grid-cols-1 gap-3 md:grid-cols-2">
          {items.map((item, index) => { const Icon = icons[index % icons.length]; return (
            <div key={`${item}-${index}`} className="rounded-xl border border-border bg-card p-4 shadow-sm">
              <div className="mb-3 flex h-8 w-8 items-center justify-center rounded-lg bg-secondary"><Icon className="h-4 w-4 text-primary" /></div>
              <p className="text-sm font-medium leading-relaxed text-foreground">{item}</p>
              <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-secondary"><div className="h-full w-2/3 rounded-full bg-primary/70" /></div>
            </div>
          ); })}
        </div>
      ) : <div className="mt-5 rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">{lang === 'en' ? 'Dashboard content is ready for data.' : '仪表盘已就绪，等待数据。'}</div>}
    </div>
  );
}