/**
 * ReasoningPanel — Collapsible monospace view of a model's chain-of-thought
 * captured from the SSE ``reasoning_done`` event.
 *
 * Single source of truth for the "REASONING (N CHARS)" UI used in three
 * places that all share the same styling, language strings, and Brain icon:
 *
 *   1. ``ActivitySteps`` — rendered at the bottom of the expanded step list
 *      so it shares the steps' collapse/expand behavior.
 *   2. ``SkillMessageBubble`` — rendered below the assistant bubble in the
 *      skill-agent chat panel (no step list there).
 *   3. ``BuilderMessageBubble`` — rendered below the assistant bubble in
 *      the agent-builder chat panel (no step list there).
 *
 * The component intentionally does NOT fetch or derive the reasoning
 * itself — the caller passes the already-resolved string. Empty / whitespace-
 * only / non-string inputs render nothing (so callers don't have to gate).
 *
 * Layout notes
 * ------------
 *   * The ``<details>`` is closed by default — the user opens it explicitly.
 *     This matches Claude / ChatGPT behavior and keeps the chat visually
 *     quiet until the user wants to peek.
 *   * The Brain icon (purple) sits in the summary line so the block is
 *     visually distinguishable from the regular "REASONING" copy in
 *     places that already have other reasoning rail components.
 *   * The ``<pre>`` is capped at ``max-h-48`` with ``overflow-y-auto``
 *     because long chains of thought can be huge — we'd rather make the
 *     user scroll inside the panel than push the whole chat down.
 */

import { Brain } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

export default function ReasoningPanel({
  reasoning,
  className = '',
  testId = 'reasoning-panel',
}) {
  const { lang } = useLanguage();
  const isEn = lang === 'en';

  // Empty / whitespace-only / non-string inputs render nothing.
  // Callers don't have to gate — passing `undefined` from a missing
  // field is the common case.
  if (typeof reasoning !== 'string' || reasoning.trim().length === 0) {
    return null;
  }

  return (
    <details
      data-testid={testId}
      className={`text-xs text-muted-foreground ${className}`}
    >
      <summary className="cursor-pointer list-none font-mono text-[10px] uppercase tracking-wide opacity-70 hover:opacity-100 transition-opacity flex items-center gap-1.5">
        <Brain className="h-3 w-3 shrink-0 text-purple-500/80" />
        <span>
          {isEn ? 'Reasoning' : '推理'} ({reasoning.length}{' '}
          {isEn ? 'chars' : '字'})
        </span>
      </summary>
      <pre className="font-mono bg-secondary/30 px-3 py-2 rounded mt-1 whitespace-pre-wrap max-h-48 overflow-y-auto">
        {reasoning}
      </pre>
    </details>
  );
}