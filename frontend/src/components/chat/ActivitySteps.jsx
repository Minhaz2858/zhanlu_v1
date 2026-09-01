/**
 * ActivitySteps — Inline Claude-style numbered activity steps.
 *
 * Renders a vertical list of numbered activity steps inside the assistant
 * message bubble. Each step shows a circular number badge, a human-readable
 * description, and a status icon (spinner → checkmark → X).
 *
 * Phase headline:
 *   When the backend streams `phase` SSE events, the latest one is passed
 *   via the `phase` prop ({ state, verb, title }) and rendered as a
 *   spinner-with-headline row above the steps — the "✳ Fathoming… /
 *   ✳ Fabricating…" pattern from claude.ai.
 *
 * Expandable step detail:
 *   Steps that carry `command` (what the tool was invoked with) and/or
 *   `output_preview` (what came back) are clickable. Expanding reveals
 *   the command in a dark code block and the output in a scrollable
 *   preview block — claude.ai's "click a step to see the bash" behavior.
 *
 * Collapse behavior:
 *   - While any step is `running`, the list is always shown (so progress is visible).
 *   - Once every step has a terminal status (`done` or `failed`), the section
 *     auto-collapses to a one-line summary like "13 steps · 9 done · 4 failed".
 *   - Clicking the summary (or its chevron when expanded) toggles the full list.
 *   - Once a user has manually expanded the list for a given message, the
 *     state is local and ephemeral — it does not persist across messages.
 *
 * Expected step shape:
 *   { number: 1, description: "Understanding your request", status: "running"|"done"|"failed",
 *     tool_name?: string, detail?: string, command?: string, output_preview?: string,
 *     artifact_id?: string }
 *
 * Reasoning panel (optional, via `reasoning` prop):
 *   When the backend streams a model's chain-of-thought via the
 *   ``reasoning_done`` SSE event, ``MessageBubble`` passes the
 *   captured text as ``reasoning``. We render it at the bottom of the
 *   expanded step list as a small collapsible block — so when the
 *   steps auto-collapse to a one-line summary, the reasoning is also
 *   hidden, and the user only sees both by clicking the summary.
 *
 * Returns null when `steps` is empty, null, or undefined (backward compat).
 */

import { useState } from 'react';
import { Loader2, CheckCircle2, XCircle, ChevronDown, ChevronRight, Sparkles, BookOpen } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import ReasoningPanel from './ReasoningPanel';

const STATUS_CONFIG = {
  running: {
    Icon: Loader2,
    iconClass: 'text-blue-500 animate-spin',
    dotClass: 'bg-blue-500',
  },
  done: {
    Icon: CheckCircle2,
    iconClass: 'text-green-500',
    dotClass: 'bg-green-500',
  },
  failed: {
    Icon: XCircle,
    iconClass: 'text-red-500',
    dotClass: 'bg-red-500',
  },
};

// Whitelist of terminal statuses. Anything else (running, unknown, missing)
// is treated as "still in progress" so the section stays expanded — we
// never auto-hide state we cannot recognize.
const TERMINAL_STATUSES = new Set(['done', 'failed']);

/** A step is expandable when it carries any detail payload worth revealing. */
function isExpandable(step) {
  return Boolean(step && (step.command || step.output_preview));
}

/**
 * `ask_data_agent` step commands embed the raw data question which may contain
 * internal table/column names. The description ("Querying the bound data
 * source") is sufficient user-facing text — hide the raw command entirely
 * for these steps so no SQL leaks.
 */
function shouldShowCommand(step) {
  if (!step?.command) return false;
  if (step.tool_name === 'ask_data_agent') return false;
  return true;
}

/**
 * Strip raw SQL/ERP internals from activity step text so the user only sees
 * human-readable language, not raw table or column
 * IDs like FMATERIALID (common in ERP systems).
 */
function sanitizeActivityText(text) {
  if (!text) return text;
  return text
    // Strip [Schema: ...] blocks entirely
    .replace(/\[Schema:[^\]]*\]/gi, '')
    // Replace erp_t_xxx / erp_v_xxx table references
    .replace(/\berp_[tv]_[a-z0-9_]+\b/gi, '[table]')
    // Replace F-prefixed column names (FMATERIALID, FREALQTY, etc.)
    .replace(/\bF[A-Z][A-Z0-9_]+\b/g, '[field]')
    // Collapse multiple spaces left behind
    .replace(/\s{2,}/g, ' ')
    .trim();
}

export default function ActivitySteps({ steps, phase, reasoning }) {
  const { lang, t } = useLanguage();
  const isEn = lang === 'en';
  const [isUserExpanded, setIsUserExpanded] = useState(false);
  // Per-step expanded state, keyed by step number. Local + ephemeral.
  const [expandedSteps, setExpandedSteps] = useState({});

  if (!steps || steps.length === 0) return null;

  // Derived: finalized = every step has a recognized terminal status.
  // Whitelist (not blacklist) so unknown statuses default to "still running"
  // and never silently collapse the section.
  const isFinalized = steps.length > 0 && steps.every((s) => TERMINAL_STATUSES.has(s.status));

  // Collapsed when message is fully loaded AND user has not clicked to expand
  const isCollapsed = isFinalized && !isUserExpanded;

  // Counts for the summary line
  const totalCount = steps.length;
  const doneCount = steps.filter((s) => s.status === 'done').length;
  const failedCount = steps.filter((s) => s.status === 'failed').length;
  const totalMs = steps.reduce((sum, s) => sum + (s.duration_ms || 0), 0);
  const totalDurStr = totalMs >= 1000
    ? `${(totalMs / 1000).toFixed(1)}s`
    : totalMs > 0 ? `${totalMs}ms` : '';

  // Localized summary copy (hardcoded lang ternary; the ReasoningSummary /
  // TracePanel cards that used this pattern were removed 2026-08-22)
  const stepWord = isEn
    ? (totalCount === 1 ? 'step' : 'steps')
    : '步';
  const doneWord = isEn ? 'done' : '成功';
  const failedWord = isEn ? 'failed' : '失败';
  const summaryText = isEn
    ? `${totalCount} ${stepWord} · ${doneCount} ${doneWord} · ${failedCount} ${failedWord}${totalDurStr ? ` · ${totalDurStr}` : ''}`
    : `${totalCount} ${stepWord} · ${doneCount} ${doneWord} · ${failedCount} ${failedWord}${totalDurStr ? ` · ${totalDurStr}` : ''}`;

  const toggleStep = (num) =>
    setExpandedSteps((prev) => ({ ...prev, [num]: !prev[num] }));

  return (
    <div className="mb-3 space-y-1.5 animate-slide-up">
      {/* Phase headline — the "✳ Fathoming…" row. Shows the latest phase
          streamed by the backend; sparkles pulse while the turn is live.
          NOTE: backend SSE ships English verb/title in phase.verb/title, but
          we deliberately ignore them and translate from phase.state so the
          headline always matches the user's UI language. */}
      {phase && phase.state && (
        <div
          data-testid="activity-phase-headline"
          className="flex items-center gap-2 px-2 py-1"
        >
          <Sparkles
            className={`h-3.5 w-3.5 shrink-0 text-amber-500 ${isFinalized ? '' : 'animate-pulse'}`}
          />
          <span className={`text-xs font-medium ${isFinalized ? 'text-muted-foreground' : 'text-foreground'}`}>
            {t.chat.phase?.[phase.state]?.verb || phase.verb || phase.state}
          </span>
          {(t.chat.phase?.[phase.state]?.title || phase.title) && (
            <span className="text-xs text-muted-foreground truncate">
              {t.chat.phase?.[phase.state]?.title || phase.title}
            </span>
          )}
        </div>
      )}

      {/* Toggle header — only rendered when the message is fully loaded.
          Doubles as the summary line (when collapsed) and as a collapse
          control (when expanded). Hidden while any step is still running. */}
      {isFinalized && (
        <button
          type="button"
          onClick={() => setIsUserExpanded((v) => !v)}
          aria-expanded={!isCollapsed}
          aria-controls="activity-steps-list"
          data-testid="activity-steps-toggle"
          className="flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
        >
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-green-100 text-green-600 ring-1 ring-green-200 text-[10px] font-semibold leading-none">
            {totalCount}
          </span>
          <span className="flex-1 text-left leading-5">{summaryText}</span>
          <ChevronDown
            className={`h-3.5 w-3.5 shrink-0 transition-transform ${isCollapsed ? '' : 'rotate-180'}`}
          />
        </button>
      )}

      {/* Full step list — hidden when collapsed to a summary */}
      {!isCollapsed && (
        <div id="activity-steps-list" data-testid="activity-steps-list" className="space-y-1.5">
          {steps.map((step, idx) => {
            const { Icon, iconClass } = STATUS_CONFIG[step.status] || STATUS_CONFIG.running;
            const isRunning = step.status === 'running';
            const isDone = step.status === 'done';
            const isFailed = step.status === 'failed';
            const expandable = isExpandable(step);
            const stepKey = step.number ?? idx;
            const isStepExpanded = Boolean(expandedSteps[stepKey]);
            // FIX 2026-08-23: detect skill-driven steps for KIMI-style highlighting
            const isSkillStep = step.tool_name === 'load_skill_body'
              || step.tool_name === 'Skill'
              || step.tool_name === 'skill'
              || (step.description && /skill|methodology/i.test(step.description));

            return (
              <div
                key={stepKey}
                className={`rounded-lg transition-colors hover:bg-muted/40 ${
                  isSkillStep ? 'border-l-[3px] border-l-indigo-400' : ''
                }`}
              >
                {/* Step row — clickable when it carries expandable detail */}
                <div
                  role={expandable ? 'button' : undefined}
                  tabIndex={expandable ? 0 : undefined}
                  aria-expanded={expandable ? isStepExpanded : undefined}
                  data-testid={expandable ? `activity-step-expandable-${stepKey}` : undefined}
                  onClick={expandable ? () => toggleStep(stepKey) : undefined}
                  onKeyDown={expandable ? (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      toggleStep(stepKey);
                    }
                  } : undefined}
                  className={`flex items-center gap-2.5 px-2 py-1.5 ${expandable ? 'cursor-pointer' : ''}`}
                >
                  {/* Number badge */}
                  <span
                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold leading-none transition-colors ${
                      isSkillStep && isRunning
                        ? 'bg-indigo-100 text-indigo-600 ring-1 ring-indigo-200'
                        : isRunning
                        ? 'bg-blue-100 text-blue-600 ring-1 ring-blue-200'
                        : isFailed
                          ? 'bg-red-100 text-red-600 ring-1 ring-red-200'
                          : isSkillStep
                            ? 'bg-indigo-100 text-indigo-600 ring-1 ring-indigo-200'
                            : 'bg-green-100 text-green-600 ring-1 ring-green-200'
                    }`}
                  >
                    {isSkillStep ? <BookOpen className="h-3 w-3" /> : step.number}
                  </span>

                  {/* Description */}
                  <span
                    className={`flex-1 text-xs leading-5 transition-colors ${
                      isRunning
                        ? 'text-foreground font-medium'
                        : isFailed
                          ? 'text-muted-foreground line-through'
                          : 'text-muted-foreground'
                    }`}
                  >
                    {sanitizeActivityText(step.description)}
                  </span>

                  {/* Status icon */}
                  <span className="flex shrink-0 items-center">
                    <Icon className={`h-3.5 w-3.5 ${iconClass}`} />
                  </span>

                  {/* Duration label — shows "X.Xs" when the step is done/failed */}
                  {step.duration_ms != null && (isDone || isFailed) && (
                    <span className="text-[10px] text-muted-foreground/50 shrink-0 tabular-nums">
                      {step.duration_ms >= 1000
                        ? `${(step.duration_ms / 1000).toFixed(1)}s`
                        : `${step.duration_ms}ms`}
                    </span>
                  )}

                  {/* Detail tooltip (optional) */}
                  {step.detail && (
                    <span className="text-[10px] text-muted-foreground/60 hidden sm:inline">
                      {step.detail}
                    </span>
                  )}

                  {/* Expand chevron — only for steps with hidden detail */}
                  {expandable && (
                    <span className="flex shrink-0 items-center text-muted-foreground/60">
                      {isStepExpanded
                        ? <ChevronDown className="h-3.5 w-3.5" />
                        : <ChevronRight className="h-3.5 w-3.5" />}
                    </span>
                  )}
                </div>

                {/* Expanded detail — command (dark code block, like Claude's
                    bash panel) + output preview (scrollable) */}
                {expandable && isStepExpanded && (
                  <div className="ml-9 mr-2 mb-1.5 space-y-1.5" data-testid={`activity-step-detail-${stepKey}`}>
                    {shouldShowCommand(step) && (
                      <pre className="max-h-48 overflow-auto rounded-md bg-zinc-900 px-3 py-2 text-[11px] leading-4 text-zinc-100 whitespace-pre-wrap break-words">
                        {sanitizeActivityText(step.command)}
                      </pre>
                    )}
                    {step.output_preview && (
                      <pre className={`max-h-40 overflow-auto rounded-md px-3 py-2 text-[11px] leading-4 whitespace-pre-wrap break-words ${
                        isFailed ? 'bg-red-50 text-red-700' : 'bg-muted/60 text-muted-foreground'
                      }`}>
                        {step.output_preview}
                      </pre>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          {/* Reasoning panel — sits inside the same collapsible region as
              the steps so it disappears when the user collapses to the
              "N steps · N done · N failed" summary line. Visually
              separated from the step rows with a top divider so it
              doesn't read as just another numbered step. */}
          <ReasoningPanel
            reasoning={reasoning}
            className="mt-2 border-t border-border/60 pt-1.5"
            testId="activity-steps-reasoning"
          />
        </div>
      )}
    </div>
  );
}
