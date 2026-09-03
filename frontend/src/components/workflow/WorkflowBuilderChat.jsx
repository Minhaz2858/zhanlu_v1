import { useRef, useState, useEffect } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import {
  createAgentConversation,
  listAgentConversations,
  streamAgentResponse,
} from '@/api/agentEnhanced';
import { Button } from '@/components/ui/button';
import {
  Send,
  X,
  Loader2,
  RotateCcw,
  Workflow,
  BarChart3,
  Inbox,
  Search,
  Zap,
  Bot,
  PanelTop,
} from 'lucide-react';

// Starter prompts shown in the blueprint empty state (Task 1). Each entry
// carries a lucide icon name (resolved via starterIcon below) and fully
// localized en/zh copy; clicking a card sends the prompt through the SAME
// handleSend path as typing (handleSend(prefill)).
const STARTERS = [
  {
    id: 'sales',
    icon: 'BarChart3',
    en: {
      title: 'Sales report pipeline',
      prompt:
        'A weekly sales report workflow: pull sales from the database, summarize top products and customers, and output a markdown brief.',
    },
    zh: {
      title: '销售报告流水线',
      prompt:
        '创建一个每周销售报告工作流：从数据库拉取销售数据，汇总重点产品和客户，并输出 Markdown 简报。',
    },
  },
  {
    id: 'triage',
    icon: 'Inbox',
    en: {
      title: 'Feedback triage',
      prompt:
        'Classify incoming customer feedback by topic and route urgent complaints to a separate alert branch.',
    },
    zh: {
      title: '反馈分流',
      prompt: '将客户反馈按主题分类，并把紧急投诉路由到单独的告警分支。',
    },
  },
  {
    id: 'research',
    icon: 'Search',
    en: {
      title: 'Research assistant',
      prompt: 'Research a topic with web search, summarize the findings, and save a short brief.',
    },
    zh: {
      title: '研究助手',
      prompt: '用联网搜索研究一个主题，总结发现，并保存一份简短简报。',
    },
  },
];

/**
 * Task 2 — safe block renderer for the assistant's streamed content.
 *
 * The workflow_builder agent emits structured text (section titles, bullets,
 * ```-fenced JSON/config graph snippets, **bold** / `inline code` spans), so
 * the plain whitespace-pre-wrap bubble is replaced by this tiny line-based
 * parser. Everything is returned as plain JSX children — React escapes all
 * text, and there is NO dangerouslySetInnerHTML anywhere in this file.
 */

// True when a trimmed line should be treated as monospace content: a ```
// fence, a bare JSON-ish payload ('{'), a ':::' admonition (e.g. the
// ':::decision-summary' blocks), or a '['-prefixed section the agent labels
// with json/config (e.g. '[json] workflow draft').
function isMonoLine(line) {
  const t = String(line ?? '').trim();
  return (
    t.startsWith('```') ||
    t.startsWith('{') ||
    t.startsWith(':::') ||
    (t.startsWith('[') && /json|config/i.test(t))
  );
}

// Split on **bold** and `code` tokens; plain runs are returned untouched
// (so React renders them as escaped text). No HTML strings are produced.
function renderInline(text) {
  const parts = String(text ?? '').split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return parts.map((part, idx) => {
    if (!part) return null;
    if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
      return <strong key={idx}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code
          key={idx}
          className="rounded bg-foreground/10 px-1 py-0.5 font-mono text-[0.85em]"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    return part;
  });
}

// One monospace panel: keeps code runs readable without a horizontal-scroll
// trap, tinted with a translucent foreground wash (bg-foreground/5 compiles
// to hsl(var(--foreground) / .05) because the theme vars are raw channels).
function monoPanel(code, key) {
  return (
    <pre
      key={key}
      className="overflow-x-auto whitespace-pre-wrap rounded-md bg-foreground/5 p-2 font-mono text-[11px] leading-relaxed"
    >
      {code}
    </pre>
  );
}

// Line-based block parser for an assistant bubble. Handles ``` fenced runs
// (and lone mono lines), '#/##/### ' titles, '- '/'* ' bullets, blank-line
// spacers and plain paragraphs (with renderInline for inline tokens).
function renderBlocks(content) {
  const lines = String(content ?? '').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) {
      // Blank line -> tiny spacer so paragraphs/panels breathe.
      out.push(<div key={`spacer-${i}`} aria-hidden="true" className="h-1" />);
      i += 1;
    } else if (line.startsWith('```')) {
      // Fenced run: collect everything up to the closing ``` fence.
      const buf = [];
      i += 1;
      while (i < lines.length && lines[i].trim() !== '```') {
        buf.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1; // skip the closing fence
      out.push(monoPanel(buf.join('\n'), `fence-${i}`));
    } else if (/^#{1,3}\s+/.test(line)) {
      out.push(
        <div key={`title-${i}`} className="text-xs font-semibold">
          {renderInline(line.replace(/^#{1,3}\s+/, ''))}
        </div>,
      );
      i += 1;
    } else if (/^[-*]\s+/.test(line)) {
      out.push(
        <div
          key={`bullet-${i}`}
          className="flex gap-1.5 text-[13px] leading-relaxed"
        >
          <span aria-hidden="true">•</span>
          <span className="min-w-0 flex-1">
            {renderInline(line.replace(/^[-*]\s+/, ''))}
          </span>
        </div>,
      );
      i += 1;
    } else if (isMonoLine(raw)) {
      // Lone mono line(s): group consecutive mono lines so multi-line
      // {json} payloads that were not fenced still read as one block.
      const buf = [raw];
      i += 1;
      while (i < lines.length && isMonoLine(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      out.push(monoPanel(buf.join('\n'), `mono-${i}`));
    } else {
      out.push(
        <div key={`para-${i}`} className="text-[13px] leading-relaxed">
          {renderInline(raw)}
        </div>,
      );
      i += 1;
    }
  }
  return out;
}

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
 * `metadata_` attr under the DB column name `metadata`). The check runs for
 * EVERY streamed event, so a standalone { type: 'flow_saved' } event is not
 * swallowed by the delta/done branching.
 *
 * The panel gates its own rendering on the `open` prop (returns null when
 * closed), so it can be mounted unconditionally by the parent without
 * permanently covering the canvas. Escape closes it, matching the
 * ReportSidePanel convention.
 */
export default function WorkflowBuilderChat({ workflowId, open, onClose, onFlowSaved }) {
  const { lang } = useLanguage();
  const isEn = lang === 'en';
  const [messages, setMessages] = useState([]); // { role, content, _sessionError? }
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [convId, setConvId] = useState(null);
  const [resumed, setResumed] = useState(false); // true when the panel reused an existing session
  const [startingSession, setStartingSession] = useState(false);
  const convIdRef = useRef(null);
  const abortRef = useRef(null);
  const scrollRef = useRef(null);

  // Start-or-resume the workflow builder session on first open — mirrors
  // createAgentConversation in Chat.jsx (agent_name 'workflow_builder',
  // metadata.workflow_id so the backend persistence branch knows which
  // workflow to save, top-level title per the documented contract). If a
  // session for this workflow already exists (listAgentConversations filtered
  // by workflow_id), resume the LATEST one: reuse its conversation id and
  // hydrate its message history so reopening the panel continues where the
  // user left off. The header "New session" button starts fresh instead.
  // Cleanup aborts any in-flight stream when the panel closes or the
  // component unmounts, so we never setState after unmount.
  useEffect(() => {
    if (!open || convIdRef.current) return;
    // Drop any previous session-start error bubble so retrying (close +
    // reopen) does not stack duplicate error bubbles.
    setMessages((prev) => prev.filter((m) => !m._sessionError));
    let cancelled = false;
    async function startOrResume() {
      try {
        const existing = await listAgentConversations('workflow_builder', {
          workflowId,
          limit: 1,
        });
        if (cancelled) return;
        if (existing && existing.length > 0) {
          // Most-recent-first order → [0] is the latest session.
          const conv = existing[0];
          convIdRef.current = conv.id;
          setConvId(conv.id);
          setResumed(true);
          setMessages(
            (conv.messages || [])
              .map((m) => ({
                role: m.role === 'user' ? 'user' : 'assistant',
                content: typeof m.content === 'string' ? m.content : '',
              }))
              .filter((m) => m.content),
          );
          return;
        }
        const conv = await createAgentConversation(
          'workflow_builder',
          { workflow_id: workflowId },
          'Workflow builder',
        );
        if (cancelled) return;
        convIdRef.current = conv.id;
        setConvId(conv.id);
        setResumed(false);
      } catch (err) {
        if (cancelled) return;
        console.error('[WorkflowBuilderChat] conversation create failed', err);
        setMessages((prev) => [
          ...prev,
          {
            role: 'assistant',
            content: isEn
              ? 'Sorry, I could not start the workflow builder session. Please try again.'
              : '抱歉，无法启动工作流构建会话，请重试。',
            _sessionError: true,
          },
        ]);
      }
    }
    startOrResume();
    return () => {
      cancelled = true;
      if (abortRef.current) {
        abortRef.current.abort();
        abortRef.current = null;
      }
    };
  }, [open, workflowId]);

  // Start a brand-new build session for this workflow: create a fresh
  // conversation and clear the panel (history stays on the server under the
  // old conversation, reachable again on next open via resume). Guarded
  // while a session is starting or a stream is in flight.
  async function startNewSession() {
    if (startingSession || streaming || !workflowId) return;
    setStartingSession(true);
    try {
      const conv = await createAgentConversation(
        'workflow_builder',
        { workflow_id: workflowId },
        'Workflow builder',
      );
      convIdRef.current = conv.id;
      setConvId(conv.id);
      setResumed(false);
      setMessages([]);
    } catch (err) {
      console.error('[WorkflowBuilderChat] new session create failed', err);
    } finally {
      setStartingSession(false);
    }
  }

  // Abort any in-flight stream when the panel closes.
  useEffect(() => {
    if (open) return;
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setStreaming(false);
  }, [open]);

  // Close on Escape while open — same convention as ReportSidePanel.
  // Escape closes regardless of focus (even while typing in the input);
  // Enter-to-send is unaffected.
  useEffect(() => {
    if (!open || !onClose) return;
    const handler = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [open, onClose]);

  // Auto-scroll the message list to the newest message.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  async function handleSend(prefill) {
    const text = (prefill ?? input).trim();
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
    const friendlyErr = isEn
      ? 'Sorry, the connection was interrupted. Please try again.'
      : '抱歉，连接中断了，请重试。';

    try {
      for await (const evt of streamAgentResponse(
        conv,
        { role: 'user', content: text },
        controller.signal,
      )) {
        // flow_saved — the backend persisted the draft graph. The signal
        // may arrive as a typed event, a status field, or a conversation
        // metadata flag (workflow_flow_saved). Evaluated for EVERY
        // streamed event (not just 'done') so a standalone
        // { type: 'flow_saved' } SSE event closes the panel and tells the
        // canvas to reload instead of falling through unhandled.
        const convMeta = evt.conversation?.metadata || evt.conversation?.metadata_ || {};
        const flowSaved =
          evt.type === 'flow_saved' ||
          evt.status === 'flow_saved' ||
          convMeta.workflow_flow_saved === true;
        if (flowSaved && onFlowSaved) {
          onFlowSaved();
          if (onClose) onClose();
          break;
        }

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
        }
      }
      // Normal stream exit without an explicit 'done' event — fill the
      // placeholder bubble with whatever content accumulated (or the
      // friendly fallback) so it is not stuck empty.
      setMessages((prev) => {
        const next = [...prev];
        next[next.length - 1] = {
          role: 'assistant',
          content: (next[next.length - 1]?.content || fullContent) || friendlyErr,
        };
        return next;
      });
    } catch (err) {
      // AbortError → user closed the panel mid-stream; stay quiet.
      if (err && (err.name === 'AbortError' || err.code === 20)) return;
      console.error('[WorkflowBuilderChat stream error]', err);
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

  // Resolve the icon-name strings in STARTERS to lucide components.
  const starterIcon = { BarChart3, Inbox, Search };

  // Gate rendering on `open` so an unconditional mount (Task 8) does not
  // permanently cover the canvas. All hooks run above, so this is safe.
  if (!open) return null;

  return (
    <div className="fixed inset-y-0 right-0 z-50 flex w-[400px] flex-col border-l bg-background shadow-xl">
      {/* Header — Workflow mark tile + title column + session status line. */}
      <div className="flex items-center gap-2.5 border-b px-4 py-3">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-gradient-to-br from-primary/90 to-primary/60 text-primary-foreground">
          <Workflow className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold leading-tight">
            {isEn ? 'Build with AI' : 'AI 构建'}
          </h2>
          <p className="truncate text-[10px] leading-tight text-muted-foreground">
            {resumed
              ? isEn
                ? 'Resumed session'
                : '已恢复会话'
              : isEn
                ? 'AI workflow builder'
                : 'AI 工作流构建'}
          </p>
        </div>
        {resumed && !streaming && (
          <Button
            variant="ghost"
            size="sm"
            onClick={startNewSession}
            disabled={startingSession}
            title={isEn ? 'Start a new session' : '新建会话'}
            className="mr-1"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {isEn ? 'New' : '新建'}
          </Button>
        )}
        <Button variant="ghost" size="icon" onClick={onClose} title={isEn ? 'Close (Esc)' : '关闭 (Esc)'}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Message list */}
      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {messages.length === 0 && (
          <div className="flex flex-col gap-3">
            {/* Blueprint mark + mini-pipeline: trigger → agent → output, the
                last node pulsing to signal "waiting for your idea". */}
            <div className="flex flex-col items-center gap-2 rounded-lg border bg-background px-3 py-4">
              <span className="flex h-8 w-8 items-center justify-center rounded-full border bg-muted text-primary">
                <Workflow className="h-4 w-4" />
              </span>
              <div className="flex items-center gap-1.5">
                <span className="flex h-7 w-7 items-center justify-center rounded-md border bg-muted text-muted-foreground">
                  <Zap className="h-3.5 w-3.5" />
                </span>
                <span aria-hidden="true" className="h-px w-4 bg-border" />
                <span className="flex h-7 w-7 items-center justify-center rounded-md border bg-muted text-muted-foreground">
                  <Bot className="h-3.5 w-3.5" />
                </span>
                <span aria-hidden="true" className="h-px w-4 bg-border" />
                <span className="relative flex h-7 w-7 items-center justify-center rounded-md border bg-muted text-muted-foreground">
                  <PanelTop className="h-3.5 w-3.5" />
                  <span
                    aria-hidden="true"
                    className="absolute -right-1 -top-1 h-2 w-2 animate-pulse rounded-full bg-primary"
                  />
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                {isEn ? "Describe it — I'll draft the workflow" : '描述它，我来起草工作流'}
              </p>
            </div>

            {/* Starter prompt cards — same send path as typing. */}
            <div className="flex flex-col gap-2">
              {STARTERS.map((s) => {
                const Icon = starterIcon[s.icon];
                const t = isEn ? s.en : s.zh;
                return (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => handleSend(t.prompt)}
                    className="flex w-full items-start gap-2.5 rounded-md border bg-background p-2.5 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border bg-muted text-muted-foreground">
                      <Icon className="h-3.5 w-3.5" />
                    </span>
                    <span className="flex min-w-0 flex-col gap-0.5">
                      <span className="text-xs font-semibold text-foreground">{t.title}</span>
                      <span className="truncate text-xs text-muted-foreground">{t.prompt}</span>
                    </span>
                  </button>
                );
              })}
            </div>

            <p className="text-center text-xs text-muted-foreground">
              {isEn ? '…or describe your own workflow below.' : '…或在下方向我描述你的工作流。'}
            </p>
          </div>
        )}
        {messages.map((m, i) => {
          // While the first delta is still in flight the placeholder bubble
          // has no content yet — show a pulsing caret inside the assistant
          // card so the panel reads as "thinking", not broken.
          const showCaret = streaming && i === messages.length - 1 && !m.content;
          if (m._sessionError) {
            // Session-start failures stay plain text but get a destructive
            // tone (border-destructive/30 + bg-destructive/5 compile to
            // hsl(var(--destructive) / .3 / .05) — see index.css channels).
            return (
              <div
                key={i}
                className="max-w-[85%] whitespace-pre-wrap rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive"
              >
                {m.content}
              </div>
            );
          }
          if (m.role === 'user') {
            return (
              <div
                key={i}
                className="ml-auto max-w-[85%] whitespace-pre-wrap rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground"
              >
                {m.content}
              </div>
            );
          }
          return (
            <div
              key={i}
              className="w-full max-w-[85%] space-y-1 rounded-lg bg-muted px-3 py-2 text-foreground"
            >
              {m.content ? (
                renderBlocks(m.content)
              ) : showCaret ? (
                <span aria-hidden="true" className="inline-block animate-pulse text-[1em] leading-none">
                  ▍
                </span>
              ) : null}
            </div>
          );
        })}
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
        <Button size="icon" onClick={() => handleSend()} disabled={streaming} title={isEn ? 'Send' : '发送'}>
          {streaming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
        </Button>
      </div>
    </div>
  );
}
