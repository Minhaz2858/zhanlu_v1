import { useState, useEffect } from 'react';
import { Bot } from 'lucide-react';

const MESSAGES = {
  zh: ['正在分析你的需求…', '正在草拟五层提示词…', '正在推荐技能与能力…', '正在配置护栏与可观测性…', '正在保存智能体配置…'],
  en: ['Analyzing your requirements…', 'Drafting the five-layer prompt…', 'Recommending skills & capabilities…', 'Configuring guardrails & observability…', 'Saving the agent configuration…'],
};

export default function ThinkingIndicator({ lang = 'en' }) {
  const msgs = MESSAGES[lang] || MESSAGES.en;
  const [idx, setIdx] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => {
      setIdx((i) => (i < msgs.length - 1 ? i + 1 : i));
    }, 2400);
    return () => clearInterval(timer);
  }, [msgs.length]);

  return (
    <div className="flex animate-slide-up items-center gap-2.5">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border bg-secondary">
        <Bot className="h-4 w-4 text-primary" strokeWidth={1.5} />
      </div>
      <div className="flex items-center gap-2.5 rounded-full border border-border bg-card px-4 py-2.5">
        <div className="flex items-center gap-1">
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-primary/70" style={{ animationDelay: '0ms' }} />
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-primary/70" style={{ animationDelay: '160ms' }} />
          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-primary/70" style={{ animationDelay: '320ms' }} />
        </div>
        <span className="text-[13px] font-medium text-muted-foreground transition-all duration-500">{msgs[idx]}</span>
      </div>
    </div>
  );
}