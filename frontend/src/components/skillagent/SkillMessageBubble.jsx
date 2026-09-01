import { useState } from 'react';
import AgentMarkdown from '@/components/agent/AgentMarkdown';
import ReasoningPanel from '@/components/chat/ReasoningPanel';
import ArtifactCardList from '@/components/chat/ArtifactCardList';
import { Bot, User, ChevronDown, ChevronRight, CheckCircle2, XCircle, Loader2, Wrench, ShieldAlert, ThumbsUp, ThumbsDown } from 'lucide-react';
import { authFetch } from '@/api/authFetch';

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

function isSkillMutationTool(name) {
  return ['create_skill', 'update_skill', 'Tool.create', 'Tool.update'].includes(String(name || ''));
}

function SkillResultCard({ result, toolName }) {
  if (!result || typeof result !== 'object') return null;
  if (!result.id || !isSkillMutationTool(toolName)) return null;
  const isUpdate = String(toolName || '').includes('update') || toolName === 'Tool.update';
  const title = isUpdate ? 'Skill updated' : 'Skill created';
  const bodyLength = String(result.skill_md || '').trim().length;

  return (
    <div className="mt-2 rounded-lg border border-primary/20 bg-primary/5 p-3 text-xs">
      <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
        <CheckCircle2 className="h-4 w-4 text-green-500" />
        <span>{title}</span>
      </div>
      <div className="space-y-1 text-muted-foreground">
        <div><span className="font-medium text-foreground">Name:</span> {result.name || 'Untitled skill'}</div>
        {result.description && <div><span className="font-medium text-foreground">Description:</span> {result.description}</div>}
        <div className="flex flex-wrap gap-2 pt-1">
          {result.category && <span className="rounded-full bg-secondary px-2 py-0.5">{result.category}</span>}
          {result.trigger && <span className="rounded-full bg-secondary px-2 py-0.5">Trigger: {result.trigger}</span>}
          {result.version && <span className="rounded-full bg-secondary px-2 py-0.5">v{result.version}</span>}
          {bodyLength > 0 && <span className="rounded-full bg-secondary px-2 py-0.5">SKILL.md {bodyLength} chars</span>}
        </div>
      </div>
    </div>
  );
}

function ToolCallDisplay({ toolCall, conversationId }) {
  const [expanded, setExpanded] = useState(!['completed', 'success', 'failed', 'error', 'rejected'].includes(toolCall.status));
  const [approving, setApproving] = useState(false);
  const status = toolCall.status || 'pending';
  const meta = STATUS_META[status] || STATUS_META.pending;
  const isFailed = ['failed', 'error', 'rejected'].includes(status);
  const isAwaitingApproval = status === 'awaiting_approval';

  let parsedArgs = toolCall.arguments_string;
  try { parsedArgs = JSON.parse(toolCall.arguments_string); } catch { /* keep raw */ }
  let parsedResults = toolCall.results;
  if (typeof parsedResults === 'string') {
    try { parsedResults = JSON.parse(parsedResults); } catch { /* keep raw */ }
  }

  const name = toolCall.name || 'tool';
  const proj = toolCall.display_projection || {};
  const hideDetails = proj.hide_details && proj.details_redacted;

  async function handleApproval(approved) {
    setApproving(true);
    try {
      const approvalId = toolCall.approval_id;
      if (!approvalId) return;
      const endpoint = approved ? 'approve' : 'reject';
      const body = approved
        ? { reviewed_by: 'user' }
        : { reviewed_by: 'user', reason: 'Rejected by user' };
      await authFetch(`/api/governance/approvals/${approvalId}/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      // Resume the conversation — the backend will continue the LLM loop
      if (conversationId) {
        await authFetch(`/api/apps/default/agents/conversations/${conversationId}/resume`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
        });
      }
    } catch (e) {
      console.error('Approval action failed:', e);
    } finally {
      setApproving(false);
    }
  }

  if (hideDetails) {
    return (
      <div className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
        <meta.icon className={`h-3.5 w-3.5 ${meta.className}`} />
        <span>{status === 'success' || status === 'completed' ? (proj.label || 'Done') : (proj.active_label || meta.label)}</span>
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
              <pre className="overflow-x-auto rounded bg-background/60 p-2 font-mono text-[11px] text-foreground">{typeof parsedArgs === 'string' ? parsedArgs : JSON.stringify(parsedArgs, null, 2)}</pre>
            </div>
          )}
          {isAwaitingApproval && toolCall.reason && (
            <div className="rounded bg-blue-500/10 p-2 text-[11px] text-blue-600">
              <ShieldAlert className="mr-1 inline h-3 w-3" />
              {toolCall.reason}
            </div>
          )}
          {parsedResults !== undefined && parsedResults !== null && (
            <div>
              <SkillResultCard result={parsedResults} toolName={name} />
              <div className="mb-1 text-[10px] font-medium uppercase text-muted-foreground">Result</div>
              <pre className={`overflow-x-auto rounded bg-background/60 p-2 font-mono text-[11px] ${isFailed ? 'text-red-500' : 'text-foreground'}`}>{typeof parsedResults === 'string' ? parsedResults : JSON.stringify(parsedResults, null, 2)}</pre>
            </div>
          )}
          {isAwaitingApproval && (
            <div className="flex gap-2 pt-1">
              <button
                onClick={() => handleApproval(true)}
                disabled={approving}
                className="inline-flex items-center gap-1 rounded-md bg-green-600 px-2.5 py-1.5 text-[11px] font-medium text-white hover:bg-green-700 disabled:opacity-50"
              >
                <ThumbsUp className="h-3 w-3" />
                {approving ? '...' : 'Approve'}
              </button>
              <button
                onClick={() => handleApproval(false)}
                disabled={approving}
                className="inline-flex items-center gap-1 rounded-md bg-red-600 px-2.5 py-1.5 text-[11px] font-medium text-white hover:bg-red-700 disabled:opacity-50"
              >
                <ThumbsDown className="h-3 w-3" />
                {approving ? '...' : 'Reject'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function SkillMessageBubble({ message, conversationId, onArtifactPreview, onOptionSelect }) {
  const isUser = message.role === 'user';
  const toolCalls = message.tool_calls || [];

  // P0: derive trace — prefer live trace_steps (incremental from SSE),
  // then backend trace, then fall back to deriving from tool_calls
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
        {isUser ? <User className="h-4 w-4" /> : <Wrench className="h-4 w-4" />}
      </div>
      <div className={`flex max-w-[85%] flex-col ${isUser ? 'items-end' : 'items-start'}`}>
        {isUser ? (
          <div className="whitespace-pre-wrap rounded-2xl rounded-tr-sm bg-secondary px-4 py-2.5 text-sm text-foreground">{message.content}</div>
        ) : (
          <div className="rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-2.5 text-sm text-foreground">
            {message.content && <AgentMarkdown onOptionSelect={onOptionSelect} multiSelect={true}>{message.content}</AgentMarkdown>}
            {toolCalls.map((tc, i) => <ToolCallDisplay key={i} toolCall={tc} conversationId={conversationId} />)}
            <ArtifactCardList artifacts={message.artifacts} onPreview={onArtifactPreview} />
          </div>
        )}
        {/* P0: live reasoning rail — collapsible monospace view of the
            model's reasoning captured from the SSE reasoning_done event.
            Uses the shared ReasoningPanel so styling + language strings
            stay in sync with the main chat. */}
        {!isUser && (
          <ReasoningPanel
            reasoning={message.reasoning}
            className="mt-3"
            testId="skill-reasoning-panel"
          />
        )}
      </div>
    </div>
  );
}