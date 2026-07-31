import { useState, useMemo } from 'react';
import {
  Sparkles, Plus, Check, Database, Code, Clock, Wifi, FileText, Server, Cloud,
  Cpu, Gauge, Shield, Zap, Workflow, Settings, Bell, Link, Key, AlertTriangle,
  Boxes, Network, HardDrive, Activity, BarChart3, GitBranch, ChevronRight,
} from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

const ICON_MAP = {
  database: Database, db: Database,
  api: Code, code: Code, interface: Code, endpoint: Code,
  clock: Clock, time: Clock, history: Clock, schedule: Clock,
  wifi: Wifi, signal: Wifi, stream: Wifi, realtime: Wifi,
  file: FileText, document: FileText, csv: FileText, report: FileText,
  server: Server, host: Server,
  cloud: Cloud,
  cpu: Cpu, processor: Cpu,
  gauge: Gauge, meter: Gauge,
  shield: Shield, security: Shield, safety: Shield,
  zap: Zap, energy: Zap, power: Zap,
  workflow: Workflow, flow: Workflow, process: Workflow,
  settings: Settings, config: Settings, gear: Settings,
  bell: Bell, alert: Bell, notification: Bell, alarm: Bell,
  link: Link, connect: Link, integration: Link,
  key: Key, auth: Key, credential: Key,
  warning: AlertTriangle, risk: AlertTriangle, error: AlertTriangle,
  boxes: Boxes, inventory: Boxes, stock: Boxes,
  network: Network,
  harddrive: HardDrive, storage: HardDrive, disk: HardDrive,
  activity: Activity, monitor: Activity, status: Activity,
  chart: BarChart3, analytics: BarChart3, graph: BarChart3,
  branch: GitBranch, version: GitBranch,
};

function getIcon(name) {
  if (!name) return null;
  return ICON_MAP[String(name).toLowerCase()] || null;
}

/**
 * ClarifyBatchForm — renders multiple clarify questions as a single
 * vertical form so the user can answer them all at once and submit.
 *
 * Layout: options are stacked vertically (one row per option) to keep
 * things compact, scannable, and friendly to many options. Each question
 * is a clearly separated section with a header + body.
 *
 * Props:
 *   block    {object}  { intro?, questions: [{ prompt, subtext, options: [{label, desc, icon}] }] }
 *   onSubmit {func}    Called with the assembled answer text.
 */
export default function ClarifyBatchForm({ block, onSubmit }) {
  const { t } = useLanguage();
  const questions = Array.isArray(block?.questions) ? block.questions : [];
  const intro = block?.intro || '';

  // answers[questionIndex] = { type: 'option'|'custom', label?: string, custom?: string }
  const [answers, setAnswers] = useState(() => questions.map(() => null));
  const [customInputs, setCustomInputs] = useState(() => questions.map(() => ''));
  const [customMode, setCustomMode] = useState(() => questions.map(() => false));

  const allAnswered = useMemo(
    () => answers.every((a) => a !== null && (a.type === 'option' || (a.type === 'custom' && a.custom?.trim()))),
    [answers],
  );

  function pickOption(qIdx, label) {
    setAnswers((prev) => {
      const next = [...prev];
      next[qIdx] = { type: 'option', label };
      return next;
    });
  }

  function toggleCustom(qIdx) {
    setCustomMode((prev) => {
      const next = [...prev];
      next[qIdx] = !next[qIdx];
      return next;
    });
    setAnswers((prev) => {
      const next = [...prev];
      if (!customMode[qIdx]) next[qIdx] = null;
      return next;
    });
  }

  function setCustom(qIdx, text) {
    setCustomInputs((prev) => {
      const next = [...prev];
      next[qIdx] = text;
      return next;
    });
    setAnswers((prev) => {
      const next = [...prev];
      next[qIdx] = text.trim() ? { type: 'custom', custom: text } : null;
      return next;
    });
  }

  function handleSubmit() {
    if (!allAnswered) return;
    const lines = [];
    if (intro) lines.push(intro);
    questions.forEach((q, i) => {
      const a = answers[i];
      const label = a.type === 'option' ? a.label : a.custom;
      lines.push(`${i + 1}. ${q.prompt}`);
      lines.push(`   → ${label}`);
    });
    onSubmit?.(lines.join('\n'));
  }

  if (questions.length === 0) return null;

  return (
    <div className="mt-3 space-y-3">
      {/* Intro / header */}
      <div className="flex items-center gap-2">
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-foreground text-background">
          <Sparkles className="h-3 w-3" />
        </span>
        <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">
          {t.chat.clarifyBatch?.tag || t.chat.clarify.tag}
        </span>
        <span className="text-[11px] text-muted-foreground">
          {questions.length} {t.chat.clarifyBatch?.questions || 'questions'}
        </span>
      </div>
      {intro && (
        <p className="text-sm font-medium leading-relaxed text-foreground">{intro}</p>
      )}

      {/* Questions — each is its own clearly separated card */}
      {questions.map((q, qIdx) => {
        const ans = answers[qIdx];
        const isCustom = customMode[qIdx];
        return (
          <div
            key={qIdx}
            className="overflow-hidden rounded-lg border border-border bg-card"
          >
            {/* Question header */}
            <div className="flex items-start gap-2.5 border-b border-border bg-secondary/40 px-3 py-2.5">
              <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-foreground">
                {qIdx + 1}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-semibold leading-snug text-foreground">{q.prompt}</p>
                {q.subtext && <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">{q.subtext}</p>}
              </div>
            </div>

            {/* Vertical options list */}
            <div className="divide-y divide-border">
              {(q.options || []).map((opt, oIdx) => {
                const letter = String.fromCharCode(65 + oIdx);
                const Icon = getIcon(opt.icon);
                const isSelected = ans?.type === 'option' && ans.label === opt.label;
                return (
                  <button
                    key={oIdx}
                    onClick={() => pickOption(qIdx, opt.label)}
                    className={`flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors ${
                      isSelected
                        ? 'bg-primary/10'
                        : 'hover:bg-secondary/40'
                    }`}
                  >
                    <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded text-[11px] font-semibold transition-colors ${
                      isSelected
                        ? 'bg-primary text-primary-foreground'
                        : 'bg-secondary text-foreground'
                    }`}>
                      {isSelected ? <Check className="h-3 w-3" /> : letter}
                    </span>
                    {Icon && (
                      <Icon className={`h-3.5 w-3.5 shrink-0 ${isSelected ? 'text-primary' : 'text-muted-foreground'}`} />
                    )}
                    <div className="min-w-0 flex-1">
                      <div className={`text-[13px] font-medium leading-tight ${isSelected ? 'text-primary' : 'text-foreground'}`}>
                        {opt.label}
                      </div>
                      {opt.desc && (
                        <div className="mt-0.5 text-[11px] leading-tight text-muted-foreground">
                          {opt.desc}
                        </div>
                      )}
                    </div>
                  </button>
                );
              })}
            </div>

            {/* Custom / Other */}
            {!isCustom ? (
              <button
                onClick={() => toggleCustom(qIdx)}
                className="flex w-full items-center justify-center gap-1 border-t border-border bg-secondary/20 px-3 py-1.5 text-[11px] text-muted-foreground transition-colors hover:bg-secondary/40 hover:text-foreground"
              >
                <Plus className="h-3 w-3" /> {t.chat.clarify.other} · {t.chat.clarify.otherDesc}
              </button>
            ) : (
              <div className="space-y-1 border-t border-border bg-secondary/20 px-3 py-2">
                <input
                  type="text"
                  value={customInputs[qIdx]}
                  onChange={(e) => setCustom(qIdx, e.target.value)}
                  placeholder={t.chat.clarifyBatch?.customPh || 'Type your answer…'}
                  autoFocus
                  className="w-full rounded-md border border-border bg-card px-2.5 py-1.5 text-[13px] text-foreground placeholder:text-muted-foreground focus:border-primary/40 focus:outline-none focus:ring-1 focus:ring-primary/30"
                />
                <button
                  onClick={() => toggleCustom(qIdx)}
                  className="text-[11px] text-muted-foreground hover:text-foreground"
                >
                  ← {t.chat.clarifyBatch?.backToOptions || 'Back to options'}
                </button>
              </div>
            )}
          </div>
        );
      })}

      {/* Submit button */}
      <button
        onClick={handleSubmit}
        disabled={!allAnswered}
        className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-opacity disabled:opacity-40"
      >
        {t.chat.clarifyBatch?.confirm || 'Confirm'} <ChevronRight className="h-4 w-4" />
      </button>
    </div>
  );
}
