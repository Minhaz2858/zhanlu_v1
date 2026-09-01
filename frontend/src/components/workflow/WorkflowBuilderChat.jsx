import { useRef, useState, useEffect } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import { createAgentConversation, streamAgentResponse } from '@/api/agentEnhanced';
import { Button } from '@/components/ui/button';
import { Send, X, Loader2, Sparkles } from 'lucide-react';

/**
 * WorkflowBuilderChat — right slide-over chat panel for the workflow canvas.
 *
 * The user describes a workflow in plain language; the `workflow_builder`
 * agent proposes a graph, the backend validates + saves it as a draft, and
 * emits `flow_saved` — at which point this panel toasts, closes, and calls
 * `onFlowSaved()` so WorkflowCanvasPage can reload the graph.
 *
 * API contract (mirrors the Chat page):
 *   - createAgentConversation('workflow_builder', { workflow_id }, 'Workflow builder')
 *   - streamAgentResponse(convId, { role: 'user', content }, signal) — v3 SSE
 *
 * flow_saved detection is defensive because the backend may surface it as a
 * typed SSE event OR as a flag on the done-event conversation metadata
 * (conv.metadata.workflow_flow_saved — the model serializes the Python
 * `metadata_` attr under the DB column name `metadata`).
 */
export default function WorkflowBuilderChat({ workflowId, open, onClose, onFlowSaved }) {
  const { lang } = useLanguage();
  const isEn = lang === 'en';
  const [messages, setMessages] = useState([]); // { role, content }
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [convId, setConvId] = useState(null);
  const convIdRef = useRef(null);
  const abortRef = useRef(null);
  const scrollRef = useRef(null);

  // Create the agent conversation on first open — mirrors
  // createAgentConversation in Chat.jsx (agent_name 'workflow_builder',
  // metadata.workflow_id so the backend persistence branch knows which
  // workflow to save, top-level title per the documented contract).
  useEffect(() => {
    if (!open || convIdRef.current) return;
    let cancelled = false;
    createAgentConversation(
      'workflow_builder',
      { workflow_id: workflowId },
      'Workflow builder',
    )
      .then((conv) => {
        if (cancelled) return;
        convIdRef.current = conv.id;
        setConvId(conv.id);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error('[WorkflowBuilderChat] conversation create failed', err);
        setMessages((prev) => [
          ...prev,
          {
            role: 'assistant',
            content: isEn
              ? 'Sorry, I could not start the workflow builder session. Please try again.'
              : '抱歉，无法启动工作流构建会话，请重试。',
          },
        ]);
      });
    return () => { cancelled = true; };
  }, [open, workflowId]);

  // Abort any in-flight stream when the panel closes.
  useEffect(() => {
    if (open) return;
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setStreaming(false);
  }, [open]);

  // Auto-scroll the message list to the newest message.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  async function handleSend() {
    const text = input.trim();
    if (!text || streaming) return;
    const conv = convIdRef.current || convId;
    if (!conv) return;

    setInput('');
    setStreaming(true);
    setMessages((prev) => [...prev, { role: 'user', content: text }]);
    // Placeholder assistant bubble — filled in by stream deltas.
    setMessages((prev) => [...prev, { role: 'assistant', content: '' }]);

    const controller = new AbortController();
    abortRef.current = controller;
    let fullContent = '';

    try {
      for await (const evt of streamAgentResponse(
        conv,
        { role: 'user', content: text },
        controller.signal,
      )) {
        if (evt.type === 'delta' && typeof evt.content === 'string') {
          fullContent += evt.content;
          setMessages((prev) => {
            const next = [...prev];
            next[next.length - 1] = { role: 'assistant', content: fullContent };
            return next;
          });
        } else if (evt.type === 'error') {
          console.error('[WorkflowBuilderChat stream error]', evt.message || evt);
          const friendlyErr = isEn
            ? 'Sorry, I hit an error while responding. Please try again.'
            : '抱歉，回复时出错了，请重试。';
          fullContent = fullContent || friendlyErr;
          setMessages((prev) => {
            const next = [...prev];
            next[next.length - 1] = { role: 'assistant', content: fullContent };
            return next;
          });
        } else if (evt.type === 'done') {
          if (typeof evt.content === 'string' && evt.content.length > fullContent.length) {
            fullContent = evt.content;
          }
          setMessages((prev) => {
            const next = [...prev];
            next[next.length - 1] = { role: 'assistant', content: fullContent };
            return next;
          });
          // flow_saved — the backend persisted the draft graph. The signal
          // may arrive as a typed event, a status field, or a conversation
          // metadata flag (workflow_flow_saved). Any of them closes the
          // panel and tells the canvas to reload.
          const convMeta = evt.conversation?.metadata || evt.conversation?.metadata_ || {};
          const flowSaved =
            evt.type === 'flow_saved' ||
            evt.status === 'flow_saved' ||
            convMeta.workflow_flow_saved === true;
          if (flowSaved && onFlowSaved) {
            onFlowSaved();
            if (onClose) onClose();
          }
        }
      }
    } catch (err) {
      // AbortError → user closed the panel mid-stream; stay quiet.
      if (err && (err.name === 'AbortError' || err.code === 20)) return;
      console.error('[WorkflowBuilderChat stream error]', err);
      const friendlyErr = isEn
        ? 'Sorry, the connection was interrupted. Please try again.'
        : '抱歉，连接中断了，请重试。';
      setMessages((prev) => {
        const next = [...prev];
        next[next.length - 1] = {
          role: 'assistant',
          content: (next[next.length - 1]?.content || fullContent) || friendlyErr,
        };
        return next;
      });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setStreaming(false);
    }
  }

  return (
    <div className="fixed inset-y-0 right-0 z-50 flex w-[380px] flex-col border-l bg-background shadow-xl">
      {/* Header */}
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <Sparkles className="h-4 w-4 text-primary" />
        <h2 className="flex-1 text-sm font-semibold">
          {isEn ? 'Build with AI' : 'AI 构建'}
        </h2>
        <Button variant="ghost" size="icon" onClick={onClose} title={isEn ? 'Close' : '关闭'}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Message list */}
      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {messages.length === 0 && (
          <p className="text-xs text-muted-foreground">
            {isEn
              ? 'Describe the workflow you want and the AI will draft it on this canvas.'
              : '描述你想要的工作流，AI 会在此画布上生成草稿。'}
          </p>
        )}
        {messages.map((m, i) => (
          <div
            key={i}
            className={`max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${
              m.role === 'user'
                ? 'ml-auto bg-primary text-primary-foreground'
                : 'bg-muted text-foreground'
            }`}
          >
            {m.content || (streaming && i === messages.length - 1 ? '…' : '')}
          </div>
        ))}
        {streaming && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            {isEn ? 'Building…' : '构建中…'}
          </div>
        )}
      </div>

      {/* Input row */}
      <div className="flex items-center gap-2 border-t px-4 py-3">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          disabled={streaming}
          placeholder={isEn ? 'Describe your workflow…' : '描述你的工作流…'}
          className="h-9 flex-1 rounded-md border bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
        />
        <Button size="icon" onClick={handleSend} disabled={streaming} title={isEn ? 'Send' : '发送'}>
          {streaming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
        </Button>
      </div>
    </div>
  );
}
