/**
 * ClarifyBatchSummary — surface every [[CLARIFY_BATCH]] question at once
 * so the user can answer all of them in a single step, breaking the
 * step/total deadlock visible in the "Sales Report" repro where the LLM
 * stuck at "Step 1 of 2" instead of presenting the full batch.
 *
 * The component is a drop-in for the existing ClarifyOptions when
 * ``entry.clarify_batch`` is present (an array of question objects).
 * It also degrades gracefully when the question list is empty.
 *
 * UX:
 * - Each question gets its own option list (radio-style).
 * - A "Submit all" button is the only way out; the user must answer
 *   every question before the button activates.
 * - The "Skip batch" link sends an empty answer so the LLM can fall
 *   back to defaults; this is the recovery path for the deadlock.
 */

import { useMemo, useState } from 'react';
import { ChevronRight, Send, ListChecks } from 'lucide-react';

export default function ClarifyBatchSummary({ batch, onSubmit, onSkip }) {
  const questions = useMemo(() => {
    if (!Array.isArray(batch)) return [];
    return batch.filter(
      (q) => q && typeof q === 'object' && (q.question || q.label || q.title),
    );
  }, [batch]);

  const [answers, setAnswers] = useState(() => ({}));
  const [customValue, setCustomValue] = useState(() => ({}));

  if (questions.length === 0) {
    return null;
  }

  const allAnswered = questions.every((q) => {
    const id = q.id || q.question;
    return Boolean(answers[id] && String(answers[id]).trim() !== "");
  });

  function pick(question, value) {
    const id = question.id || question.question;
    setAnswers((prev) => ({ ...prev, [id]: value }));
  }

  function pickCustom(question) {
    const id = question.id || question.question;
    const value = (customValue[id] || "").trim();
    if (!value) return;
    setAnswers((prev) => ({ ...prev, [id]: value }));
  }

  function submit() {
    if (!allAnswered) return;
    const payload = questions.map((q) => {
      const id = q.id || q.question;
      return {
        id,
        question: q.question || q.label || q.title,
        answer: answers[id],
        options: q.options || [],
      };
    });
    onSubmit?.(payload);
  }

  return (
    <div className="my-2 rounded-xl border border-amber-300/40 bg-amber-50/40 p-4 shadow-sm">
      <div className="mb-3 flex items-center gap-2 text-amber-900">
        <ListChecks className="h-4 w-4" />
        <span className="text-sm font-medium">
          Please answer all {questions.length} question
          {questions.length === 1 ? "" : "s"} at once
        </span>
      </div>

      <ol className="space-y-4">
        {questions.map((q, i) => {
          const id = q.id || q.question;
          const value = answers[id] || "";
          const options = Array.isArray(q.options) ? q.options : [];
          return (
            <li key={id || i} className="rounded-md border border-amber-200/60 bg-white/60 p-3">
              <div className="mb-2 text-sm font-medium text-foreground">
                {i + 1}. {q.question || q.label || q.title}
              </div>

              {options.length > 0 && (
                <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                  {options.map((opt, j) => {
                    const optVal = typeof opt === "string" ? opt : (opt.value || opt.label);
                    const optLabel = typeof opt === "string" ? opt : (opt.label || opt.value);
                    const checked = value === optVal;
                    return (
                      <button
                        key={`${id}-${j}`}
                        onClick={() => pick(q, optVal)}
                        className={`rounded-md border px-2.5 py-1.5 text-left text-xs transition-colors ${
                          checked
                            ? "border-primary bg-primary/10 text-primary"
                            : "border-border bg-background hover:bg-secondary"
                        }`}
                      >
                        <span className="font-medium">{optLabel}</span>
                      </button>
                    );
                  })}
                </div>
              )}

              {/* Custom value input — always available so the user can
                  type a freeform answer even if the LLM didn't enumerate
                  options. */}
              <div className="mt-2 flex items-center gap-2">
                <input
                  type="text"
                  placeholder="Or type your answer…"
                  value={customValue[id] || ""}
                  onChange={(e) =>
                    setCustomValue((prev) => ({ ...prev, [id]: e.target.value }))
                  }
                  onKeyDown={(e) => {
                    if (e.key === "Enter") pickCustom(q);
                  }}
                  className="flex-1 rounded-md border border-border bg-background px-2 py-1 text-xs focus:border-primary focus:outline-none"
                />
                <button
                  onClick={() => pickCustom(q)}
                  disabled={!(customValue[id] && customValue[id].trim())}
                  className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground hover:bg-secondary disabled:opacity-50"
                >
                  <ChevronRight className="h-3 w-3" /> Use
                </button>
              </div>

              {value && (
                <div className="mt-2 text-[11px] text-muted-foreground">
                  Selected: <span className="font-medium text-foreground">{value}</span>
                </div>
              )}
            </li>
          );
        })}
      </ol>

      <div className="mt-3 flex items-center justify-end gap-2">
        <button
          onClick={() => onSkip?.()}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-xs text-muted-foreground hover:bg-secondary"
        >
          Skip batch
        </button>
        <button
          onClick={submit}
          disabled={!allAnswered}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground shadow-sm hover:bg-primary/90 disabled:opacity-50"
        >
          <Send className="h-3.5 w-3.5" /> Submit all
        </button>
      </div>
    </div>
  );
}
