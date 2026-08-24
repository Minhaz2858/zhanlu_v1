import { useState } from 'react';
import { Bot, User, ChevronDown, ChevronRight, CheckCircle2, XCircle, Loader2, ShieldAlert, Sparkles, Wrench, ListChecks, FileText } from 'lucide-react';
import AgentMarkdown from '@/components/agent/AgentMarkdown';
import DecisionSummaryCard from '@/components/agentbuilder/DecisionSummaryCard';
import ReasoningPanel from '@/components/chat/ReasoningPanel';
import ArtifactCardList from '@/components/chat/ArtifactCardList';

// ---------------------------------------------------------------------------
// Decision-summary fence parser
// ---------------------------------------------------------------------------

const DECISION_SUMMARY_RE = /:::decision-summary\s*\n?([\s\S]*?)\n?\s*:::/;

function extractDecisionSummary(content) {
  if (!content || typeof content !== 'string') return null;
  const m = content.match(DECISION_SUMMARY_RE);
  if (!m) return null;
  try {
    const json = JSON.parse(m[1].trim());
    if (!json || typeof json !== 'object') return null;
    return { raw: m[0], json };
  } catch {
    return null;
  }
}

const STATUS_META = {
  pending: { icon: Loader2, className: 'animate-spin text-amber-500', label: 'Pending' },
  running: { icon: Loader2, className: 'animate-spin text-amber-500', label: 'Running' },
  in_progress: { icon: Loader2, className: 'animate-spin text-amber-500', label: 'In progress' },
  completed: { icon: CheckCircle2, className: 'text-green-500', label: 'Completed' },
  success: { icon: CheckCircle2, className: 'text-green-500', label: 'Success' },
  failed: { icon: XCircle, className: 'text-red-500', label: 'Failed' },
  error: { icon: XCircle, className: 'text-red-500', label: 'Error' },
  awaiting_approval: { icon: ShieldAlert, className: 'text-blue-500', label: 'Awaiting approval' },
  approved: { icon: CheckCircle2, className: 'text-green-500', label: 'Approved' },
  rejected: { icon: XCircle, className: 'text-red-500', label: 'Rejected' },
};

// ---------------------------------------------------------------------------
// Tool result helpers — convert raw JSON into friendly UI data
// ---------------------------------------------------------------------------

function safeParse(value) {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch { return value; }
}

/**
 * True if the tool call is creating/updating an agent — we render those
 * with a dedicated card UI rather than raw JSON.
 */
function isAgentMutation(toolName) {
  if (!toolName) return false;
  const n = String(toolName).toLowerCase();
  return n === 'create_agent' || n === 'update_agent'
      || n.includes('agentapp.create') || n.includes('agentapp.update');
}

/**
 * Returns true when the assistant text is the long, structured markdown
 * summary (Agent Overview table + Capabilities + Five-Layer Prompt
 * Summary + Bound Skills + Guardrails & Observability) that the
 * agent_builder streams after a successful create_agent. The compact
 * `AgentConfigCard` already conveys the same info, so we hide the
 * redundant prose from the chat to keep the completion view minimal.
 */
function isVerboseAgentSummary(content) {
  if (!content || typeof content !== 'string') return false;
  const text = content.trim();
  return /^#{1,3}\s*Agent Created Successfully\b/i.test(text)
      || /\*\*Agent Overview\*\*/i.test(text)
      || /\*\*Five-Layer Prompt Summary\*\*/i.test(text);
}

/**
 * Build a structured view of an agent from either:
 *   - the tool call arguments (when status is awaiting_approval), or
 *   - the tool call result (when status is completed/success and result
 *     is the full agent record).
 *
 * Returns { name, description, capabilities, model, project, skills,
 *          agentType, status, id } or null when nothing useful is found.
 */
function readAgentView(parsedArgs, parsedResults, toolName) {
  if (!isAgentMutation(toolName)) return null;
  // Prefer the result when it looks like an agent record
  const fromResult = parsedResults && typeof parsedResults === 'object'
    ? (parsedResults.data || parsedResults)
    : null;
  const fromArgs = parsedArgs && typeof parsedArgs === 'object' ? parsedArgs : null;

  const src = (fromResult && (fromResult.name || fromResult.capabilities)) ? fromResult : fromArgs;
  if (!src) return null;

  return {
    name: src.name || 'Untitled Agent',
    description: src.description || '',
    capabilities: Array.isArray(src.capabilities) ? src.capabilities : [],
    model: src.model || '',
    project: src.project || '',
    skills: Array.isArray(src.skills) ? src.skills : [],
    agentType: src.agent_type || '',
    status: src.status || '',
    id: fromResult?.id || fromResult?.agent_id || null,
  };
}

// ---------------------------------------------------------------------------
// AgentConfigCard — clean UI for create_agent / update_agent results
// ---------------------------------------------------------------------------

function AgentConfigCard({ view, isAwaitingApproval, isSuccess, isFailed, errorMessage }) {
  // Note: `isAwaitingApproval` is preserved as a prop for backward
  // compatibility with any leftover data from conversations that ran
  // before the approval flow was removed. For system meta-agents
  // (agent_builder, etc.) the backend now auto-creates and the card
  // transitions directly from "Creating agent…" to "Agent created
  // successfully" without any user action.
  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-border bg-card text-xs">
      <div className={`flex items-center gap-2 border-b border-border px-3 py-2 ${
        isSuccess ? 'bg-green-500/10' : isFailed ? 'bg-red-500/10' : isAwaitingApproval ? 'bg-blue-500/10' : 'bg-secondary/40'
      }`}>
        {isSuccess ? (
          <CheckCircle2 className="h-4 w-4 text-green-500" />
        ) : isFailed ? (
          <XCircle className="h-4 w-4 text-red-500" />
        ) : isAwaitingApproval ? (
          <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
        ) : (
          <Sparkles className="h-4 w-4 text-primary" />
        )}
        <div className="flex-1 min-w-0">
          <div className="font-medium text-foreground">
            {isSuccess
              ? 'Agent created successfully'
              : isFailed
                ? (errorMessage || 'Failed to create agent')
                : isAwaitingApproval
                  ? 'Creating agent…'
                  : 'Agent configuration'}
          </div>
          <div className="truncate text-[11px] text-muted-foreground">{view.name}</div>
        </div>
      </div>
      <div className="space-y-2 px-3 py-2.5">
        {view.description && (
          <div>
            <div className="mb-0.5 flex items-center gap-1 text-[10px] font-medium uppercase text-muted-foreground">
              <FileText className="h-3 w-3" /> Description
            </div>
            <div className="text-[12px] text-foreground">{view.description}</div>
          </div>
        )}
        {view.capabilities.length > 0 && (
          <div>
            <div className="mb-1 flex items-center gap-1 text-[10px] font-medium uppercase text-muted-foreground">
              <ListChecks className="h-3 w-3" /> Capabilities
            </div>
            <div className="flex flex-wrap gap-1">
              {view.capabilities.map((c, i) => (
                <span key={i} className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] text-foreground">{c}</span>
              ))}
            </div>
          </div>
        )}
        {view.skills.length > 0 && (
          <div>
            <div className="mb-1 flex items-center gap-1 text-[10px] font-medium uppercase text-muted-foreground">
              <Wrench className="h-3 w-3" /> Skills
            </div>
            <div className="flex flex-wrap gap-1">
              {view.skills.map((s, i) => (
                <span key={i} className="rounded-full bg-secondary px-2 py-0.5 text-[11px] text-foreground">{s}</span>
              ))}
            </div>
          </div>
        )}
        <div className="flex flex-wrap gap-3 text-[11px] text-muted-foreground">
          {view.model && <span><span className="font-medium text-foreground/70">Model:</span> {view.model}</span>}
          {view.agentType && <span><span className="font-medium text-foreground/70">Type:</span> {view.agentType}</span>}
          {view.project && <span><span className="font-medium text-foreground/70">Project:</span> {view.project}</span>}
          {view.status && <span><span className="font-medium text-foreground/70">Status:</span> {view.status}</span>}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToolCallDisplay — the collapsible tool-call panel for non-agent tools
// ---------------------------------------------------------------------------

function ToolCallDisplay({ toolCall, conversationId, hideInternalTools }) {
  const [expanded, setExpanded] = useState(!['completed', 'success', 'failed', 'error', 'rejected'].includes(toolCall.status));
  const status = toolCall.status || 'pending';
  const meta = STATUS_META[status] || STATUS_META.pending;
  const isFailed = ['failed', 'error', 'rejected'].includes(status);
  const isAwaitingApproval = status === 'awaiting_approval';
  const isSuccess = status === 'completed' || status === 'success';

  const parsedArgs = safeParse(toolCall.arguments_string);
  const parsedResults = safeParse(toolCall.results);

  const name = toolCall.name || 'tool';
  const proj = toolCall.display_projection || {};
  const hideDetails = proj.hide_details && proj.details_redacted;

  // ---- Agent mutation: render the friendly card instead of raw JSON -------
  const agentView = readAgentView(parsedArgs, parsedResults, name);
  if (agentView) {
    const errorMessage = (parsedResults && typeof parsedResults === 'object' && parsedResults.error) || null;
    return (
      <div className="mt-2">
        <AgentConfigCard
          view={agentView}
          isAwaitingApproval={isAwaitingApproval}
          isSuccess={isSuccess && (parsedResults?.success !== false)}
          isFailed={isFailed || (isSuccess && parsedResults?.success === false)}
          errorMessage={errorMessage}
        />
        {/* Approval flow removed: system meta-agents (agent_builder,
            etc.) now auto-create agents on the backend. The card
            transitions to the "success" state on its own. */}
      </div>
    );
  }

  // ---- hide_details mode (existing behaviour) -----------------------------
  if (hideDetails) {
    // Internal discovery-style tool calls (list_tools / search_skills /
    // list_knowledge_bases) are projected as one-line indicators. When
    // the conversation is paused on a Decision Summary review, those
    // indicators are noise — the Review card already shows the result —
    // and they tend to get stuck showing the *active* label
    // ("Searching available capabilities...") even after the tool
    // returned, which makes the agent look hung. Suppress them entirely
    // in that state.
    if (proj.is_internal && hideInternalTools) {
      return null;
    }
    // For internal tools, prefer a generic "Done" suffix over the
    // active label once the call has finished — the active label
    // ("Searching available capabilities...") is misleading after the
    // tool has already returned.
    const isDone = isSuccess || isFailed;
    const labelText = proj.is_internal
      ? (isDone ? (proj.done_label || 'Done') : (proj.active_label || meta.label))
      : (isDone ? (proj.label || 'Done') : (proj.active_label || meta.label));
    return (
      <div className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
        <meta.icon className={`h-3.5 w-3.5 ${meta.className}`} />
        <span>{labelText}</span>
        {isFailed && <span className="text-red-500">{proj.error_label || ''}</span>}
      </div>
    );
  }

  return (
    <div className="mt-2 rounded-lg border border-border bg-secondary/40 text-xs">
      <button onClick={() => setExpanded(!expanded)} className="flex w-full items-center gap-2 px-3 py-2 text-left">
        {expanded ? <ChevronDown className="h-3 w-3 text-muted-foreground" /> : <ChevronRight className="h-3 w-3 text-muted-foreground" />}
        <meta.icon className={`h-3.5 w-3.5 ${meta.className}`} />
        <span className="font-mono text-foreground">{name}</span>
        <span className={`ml-auto ${isFailed ? 'text-red-500' : isAwaitingApproval ? 'text-blue-500' : 'text-muted-foreground'}`}>{meta.label}</span>
      </button>
      {expanded && (
        <div className="space-y-2 border-t border-border px-3 py-2">
          {toolCall.arguments_string && (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase text-muted-foreground">Parameters</div>
              <pre className="overflow-x-auto rounded bg-background/60 p-2 font-mono text-[11px] text-foreground whitespace-pre-wrap break-words">{typeof parsedArgs === 'string' ? parsedArgs : JSON.stringify(parsedArgs, null, 2)}</pre>
            </div>
          )}
          {/* Approval flow removed: the generic tool-call panel no
              longer renders Approve/Reject buttons. If a tool result
              still carries an awaiting_approval status (legacy data),
              we just show the reason banner and let the next status
              update replace it. */}
          {isAwaitingApproval && toolCall.reason && (
            <div className="rounded bg-blue-500/10 p-2 text-[11px] text-blue-600">
              <ShieldAlert className="mr-1 inline h-3 w-3" />
              {toolCall.reason}
            </div>
          )}
          {parsedResults !== undefined && parsedResults !== null && (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase text-muted-foreground">Result</div>
              <pre className={`overflow-x-auto rounded bg-background/60 p-2 font-mono text-[11px] whitespace-pre-wrap break-words ${isFailed ? 'text-red-500' : 'text-foreground'}`}>{typeof parsedResults === 'string' ? parsedResults : JSON.stringify(parsedResults, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function BuilderMessageBubble({ message, onOptionSelect, conversationId, hideInternalTools, onDecisionConfirmed, onDecisionCancelled, onArtifactPreview }) {
  const isUser = message.role === 'user';
  const toolCalls = message.tool_calls || [];
  const hideAssistantText = !isUser && isVerboseAgentSummary(message.content);
  const decision = !isUser ? extractDecisionSummary(message.content) : null;
  const proseContent = decision
    ? message.content.replace(decision.raw, '').trim()
    : message.content;

  // P0: derive trace — prefer live trace_steps (incremental from SSE),
  // then backend-emitted trace, fall back to deriving from tool_calls
  const trace = (message.trace_steps && message.trace_steps.length > 0)
    ? message.trace_steps
    : (message.trace && message.trace.length > 0)
    ? message.trace
    : (toolCalls.length > 0
        ? toolCalls.map((tc, i) => ({
            step: i + 1,
            type: 'tool_call',
            title: tc.name || `Tool ${i + 1}`,
            detail: tc.status === 'completed' ? 'Completed' : tc.status || '',
            status: tc.status === 'completed' || tc.status === 'success' ? 'completed' : tc.status === 'failed' ? 'failed' : 'pending',
            duration_ms: 0,
          }))
        : []);

  return (
    <div className={`flex animate-slide-up gap-3 ${isUser ? 'flex-row-reverse' : ''}`}>
      <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${isUser ? 'bg-primary text-primary-foreground' : 'border border-border bg-secondary text-primary'}`}>
        {isUser ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
      </div>
      <div className={`flex max-w-[85%] flex-col ${isUser ? 'items-end' : 'items-start'}`}>
        {isUser ? (
          <div className="whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-secondary px-4 py-2.5 text-sm text-foreground">{message.content}</div>
        ) : (
          <>
            {proseContent && !hideAssistantText && (
              <div className="rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-2.5 text-sm text-foreground">
                <AgentMarkdown onOptionSelect={onOptionSelect}>{proseContent}</AgentMarkdown>
                {toolCalls.map((tc, i) => (
                  <ToolCallDisplay
                    key={i}
                    toolCall={tc}
                    conversationId={conversationId}
                    hideInternalTools={hideInternalTools}
                  />
                ))}
              </div>
            )}
            {/* Render tool calls outside prose bubble when assistant text is hidden */}
            {hideAssistantText && toolCalls.length > 0 && (
              <div className="mt-2 space-y-2">
                {toolCalls.map((tc, i) => (
                  <ToolCallDisplay
                    key={i}
                    toolCall={tc}
                    conversationId={conversationId}
                    hideInternalTools={hideInternalTools}
                  />
                ))}
              </div>
            )}
            {/* P0: live reasoning rail — collapsible monospace view.
                Uses the shared ReasoningPanel so styling + language
                strings stay in sync with the main chat. */}
            <ReasoningPanel
              reasoning={message.reasoning}
              className="mt-3"
              testId="builder-reasoning-panel"
            />
            {decision && (
              <div className="mt-2 w-full">
                <DecisionSummaryCard
                  payload={decision.json}
                  conversationId={conversationId}
                  onConfirmed={onDecisionConfirmed}
                  onCancel={onDecisionCancelled}
                  refineHint
                />
              </div>
            )}
            <ArtifactCardList artifacts={message.artifacts} onPreview={onArtifactPreview} />
          </>
        )}
      </div>
    </div>
  );
}
