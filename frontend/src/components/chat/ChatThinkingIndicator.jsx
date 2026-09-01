import { useState, useEffect } from 'react';
import { Bot, Sparkles, BrainCircuit, FileText } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

const STAGES = [
  { icon: Sparkles },
  { icon: BrainCircuit },
  { icon: FileText },
];

export default function ChatThinkingIndicator() {
  const { lang } = useLanguage();
  const [visibleCount, setVisibleCount] = useState(1);

  useEffect(() => {
    if (visibleCount >= STAGES.length) return;
    const timer = setTimeout(() => setVisibleCount((c) => c + 1), 1500);
    return () => clearTimeout(timer);
  }, [visibleCount]);

  const labels = lang === 'en'
    ? ['Understanding your request', 'Analyzing context and data', 'Preparing response']
    : ['正在理解您的请求', '正在分析上下文与数据', '正在准备回复'];

  return (
    <div className="flex animate-slide-up gap-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border bg-secondary text-muted-foreground">
        <Bot className="h-4 w-4" />
      </div>
      <div className="flex flex-col">
        {STAGES.slice(0, visibleCount).map((s, i) => {
          const Icon = s.icon;
          const isLast = i === visibleCount - 1;
          return (
            <div key={i} className="flex items-center gap-2.5">
              <div className="flex flex-col items-center">
                <div className={`flex h-5 w-5 items-center justify-center rounded-md ${isLast ? 'bg-primary/10' : ''}`}>
                  <Icon className={`h-3.5 w-3.5 ${isLast ? 'text-primary animate-pulse' : 'text-muted-foreground'}`} />
                </div>
                {i < visibleCount - 1 && <div className="w-px h-3 bg-border" />}
              </div>
              <span className={`text-sm transition-colors ${isLast ? 'text-foreground' : 'text-muted-foreground'}`}>
                {labels[i]}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}