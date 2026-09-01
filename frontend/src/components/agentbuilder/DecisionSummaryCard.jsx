/**
 * DecisionSummaryCard — R4 review step (read-only).
 *
 * Renders the structured draft the agent_builder emitted inside a
 * `:::decision-summary` block. The card is read-only by design — the
 * user refines the draft by chatting with Agent Builder, not by
 * editing an inline form. The user creates the agent with the
 * "Create Agent" button or dismisses with "Cancel".
 *
 * When the conversation is paused for review the chat thread is shown
 * side-by-side (see AgentBuilder.jsx two-column layout) so the user
 * can type refinements directly. After creation, a separate "Open
 * Prompt Engineering" button in the success dialog takes the user to
 * the agent config page where the full editor (system prompt, skills,
 * capabilities, tools) is available.
 *
 * The card itself uses a thin success-green left border to match the
 * rest of the design system's "ready to confirm" affordance. Visual
 * treatment is intentionally compact — chips for capabilities/skills
 * and a few metadata rows.
 */
import { useState, useEffect, useMemo } from 'react';
import { Bot, Loader2, CheckCircle2, Wrench, ListChecks, FileText, MessageSquareText, ShieldCheck } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { confirmDecision } from '@/api/agentEnhanced';

const DEFAULT_PAYLOAD = {
  name: '',
  description: '',
  project: 'global',
  capabilities: [],
  model: 'automatic',
  agent_type: 'sequential',
  skills: [],
};

function normalize(p) {
  // Defensive copy + missing-key fallbacks so the UI never crashes on
  // partial payloads (the backend parser is best-effort too).
  return { ...DEFAULT_PAYLOAD, ...(p || {}) };
}

export default function DecisionSummaryCard({ payload, conversationId, onConfirmed, onCancel, refineHint = true }) {
  const initial = useMemo(() => normalize(payload), [payload]);
  const draft = initial; // read-only — chat is the refinement surface
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    setError(null);
  }, [initial]);

  const canSubmit = draft.name && draft.name.trim().length > 0 && !submitting;

  async function handleCreate() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      // The router endpoint accepts the SDK's path style: POST /confirm-decision
      // body { action: 'create', payload: <draft> }. Use the base44 wrapper.
      const result = await confirmDecision(conversationId, {
        action: 'create',
        payload: draft,
      });
      onConfirmed?.(result);
    } catch (e) {
      setError(e?.message || 'Failed to create agent');
      setSubmitting(false);
    }
  }

  async function handleCancel() {
    setSubmitting(true);
    setError(null);
    try {
      await confirmDecision(conversationId, { action: 'cancel' });
      onCancel?.();
    } catch (e) {
      setError(e?.message || 'Failed to cancel');
    } finally {
      setSubmitting(false);
    }
  }

  const capabilities = Array.isArray(draft.capabilities) ? draft.capabilities : [];
  const skills = Array.isArray(draft.skills) ? draft.skills : [];

  return (
    <div
      data-testid="decision-summary-card"
      className="rounded-2xl border-l-[3px] border-l-emerald-500 border border-border bg-card shadow-sm"
    >
      <Card className="border-0 shadow-none">
        <CardContent className="space-y-3 px-4 py-3.5">
          {/* Header — single compact row */}
          <div className="flex items-center gap-2 rounded-lg bg-emerald-500/10 px-2.5 py-2">
            <CheckCircle2 className="h-4 w-4 text-emerald-600" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium text-foreground truncate">
                Review your agent
              </div>
              <div className="truncate text-[11px] text-muted-foreground">
                Ready to confirm — click Create Agent to save.
              </div>
            </div>
          </div>

          {/* Refine in chat hint */}
          {refineHint && (
            <div className="flex items-start gap-1.5 rounded-md border border-dashed border-primary/30 bg-primary/5 px-2.5 py-1.5 text-[11px] text-muted-foreground">
              <MessageSquareText className="mt-0.5 h-3 w-3 shrink-0 text-primary" />
              <span>
                Want to change anything? Just type in the chat — the agent
                builder will update this summary.
              </span>
            </div>
          )}

          {/* Name (always visible) */}
          <div>
            <div className="mb-0.5 flex items-center gap-1 text-[10px] font-medium uppercase text-muted-foreground">
              <Bot className="h-3 w-3" /> Name
            </div>
            <div className="text-sm font-medium text-foreground">{draft.name || 'Untitled Agent'}</div>
          </div>

          {/* Description */}
          {draft.description && (
            <div>
              <div className="mb-0.5 flex items-center gap-1 text-[10px] font-medium uppercase text-muted-foreground">
                <FileText className="h-3 w-3" /> Description
              </div>
              <div className="text-[12px] text-foreground">{draft.description}</div>
            </div>
          )}

          {/* Capabilities chips (read-only) */}
          {capabilities.length > 0 && (
            <div>
              <div className="mb-1 flex items-center gap-1 text-[10px] font-medium uppercase text-muted-foreground">
                <ListChecks className="h-3 w-3" /> Capabilities
                <span className="text-[10px] font-normal text-muted-foreground/80">{capabilities.length}</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {capabilities.map((c) => (
                  <span
                    key={c}
                    className="inline-flex items-center rounded-full bg-primary/10 px-2 py-0.5 text-[11px] text-foreground"
                  >
                    {c}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Skills chips (read-only) */}
          {skills.length > 0 && (
            <div>
              <div className="mb-1 flex items-center gap-1 text-[10px] font-medium uppercase text-muted-foreground">
                <Wrench className="h-3 w-3" /> Skills
                <span className="text-[10px] font-normal text-muted-foreground/80">{skills.length} selected</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {skills.map((s) => (
                  <span
                    key={s}
                    className="inline-flex items-center rounded-full bg-secondary px-2 py-0.5 text-[11px] text-foreground"
                  >
                    {s}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Model · Type · Project · Status metadata row */}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
            {draft.model && (
              <span><span className="font-medium text-foreground/70">Model:</span> {draft.model}</span>
            )}
            {draft.agent_type && (
              <span><span className="font-medium text-foreground/70">Type:</span> {draft.agent_type}</span>
            )}
            {draft.project && (
              <span><span className="font-medium text-foreground/70">Project:</span> {draft.project}</span>
            )}
            <span className="inline-flex items-center gap-1">
              <ShieldCheck className="h-3 w-3 text-emerald-500" />
              <span className="font-medium text-emerald-600">Ready to confirm</span>
            </span>
          </div>

          {error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-xs text-red-600">
              {error}
            </div>
          )}

          {/* Footer */}
          <div className="flex items-center justify-between gap-2 pt-1">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleCancel}
              disabled={submitting}
              data-testid="decision-summary-cancel"
              className="h-8 text-xs"
            >
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              onClick={handleCreate}
              disabled={!canSubmit}
              data-testid="decision-summary-create"
              className="h-8 gap-1.5 text-xs"
            >
              {submitting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Bot className="h-3.5 w-3.5" />
              )}
              Create Agent
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
