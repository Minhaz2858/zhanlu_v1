import { useState, useRef, useCallback, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { createAgentConversation, streamAgentResponse } from '@/api/agentEnhanced';
import useStreamState from '@/hooks/useStreamState';
import { dedupeMessagesByFingerprint } from '@/pages/messageDedup';

const STORAGE_KEY = 'zhanlu:eciscoWidgetConvId';
const AGENT_NAME = 'ecisco_bi_assistant';

let nextId = 0;
function uid() {
  nextId += 1;
  return `widget-msg-${Date.now()}-${nextId}`;
}

/**
 * Hook that manages Ecisco BI chat conversations for the dashboard widget.
 *
 * Returns:
 *   messages     – Array of message objects ({id, role, content, tool_calls, ...})
 *   isStreaming  – true while a stream is in flight
 *   loadStatus   – 'idle' | 'loading' | 'loaded' | 'error'
 *   send(text)   – Create-or-resume conversation + stream response
 *   newChat()    – Abort + clear messages + clear localStorage conv-id
 *   stop()       – Abort current stream
 */
export function useEciscoBiChat() {
  const [messages, setMessages] = useState([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [loadStatus, setLoadStatus] = useState('idle');

  const convIdRef = useRef(null);
  const genRef = useRef(0);
  const streamState = useStreamState();
  const { abortRef } = streamState;

  // ── Resume existing conversation on mount ──────────────────────────
  useEffect(() => {
    const storedId = localStorage.getItem(STORAGE_KEY);
    if (!storedId) {
      setLoadStatus('idle');
      return;
    }
    setLoadStatus('loading');
    convIdRef.current = storedId;

    base44.entities.AgentConversation.get(storedId)
      .then((conv) => {
        if (!conv || !Array.isArray(conv.messages)) {
          localStorage.removeItem(STORAGE_KEY);
          convIdRef.current = null;
          setLoadStatus('idle');
          return;
        }
        setMessages(dedupeMessagesByFingerprint(conv.messages));
        setLoadStatus('loaded');
      })
      .catch((err) => {
        console.error('[EciscoBiWidget] resume failed', err);
        localStorage.removeItem(STORAGE_KEY);
        convIdRef.current = null;
        setLoadStatus('error');
      });

    return () => {
      streamState.abort?.();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Send a message ─────────────────────────────────────────────────
  const send = useCallback(async (text) => {
    if (!text.trim()) return;
    const trimmed = text.trim().replace(/\s+/g, ' ').slice(0, 2000);
    const title = trimmed.slice(0, 60);

    const gen = ++genRef.current;

    try {
      setIsStreaming(true);

      // 1. Create or reuse conversation
      let convId = convIdRef.current;
      if (!convId) {
        const conv = await createAgentConversation(
          AGENT_NAME,
          {
            project: 'Ecisco BI',
            project_name: 'Ecisco BI',
            _source: 'market_dashboard_widget',
          },
          title,
        );
        convId = conv.id;
        convIdRef.current = convId;
        localStorage.setItem(STORAGE_KEY, convId);
      }

      if (gen !== genRef.current) return;

      // 2. Add user message
      const userMsg = { id: uid(), role: 'user', content: trimmed };
      setMessages((prev) => [...prev, userMsg]);

      // 3. Add streaming AI placeholder
      const aiMsgId = uid();
      const aiPlaceholder = {
        id: aiMsgId,
        role: 'assistant',
        content: '',
        isStreaming: true,
      };
      setMessages((prev) => [...prev, aiPlaceholder]);

      // 4. Stream response
      let fullContent = '';
      let toolCalls = [];
      let activitySteps = [];
      let finalArtifacts = [];
      let currentPhase = null;
      let reasoningText = '';
      let liveTraceSteps = [];
      let planSummary = null;

      const signal = abortRef.current?.signal;

      for await (const evt of streamAgentResponse(convId, { role: 'user', content: trimmed }, signal)) {
        if (gen !== genRef.current) break;

        if (evt.type === 'delta' && typeof evt.content === 'string') {
          fullContent += evt.content;
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, content: fullContent } : m)),
          );
        } else if (evt.type === 'tool_progress' && Array.isArray(evt.tool_calls)) {
          toolCalls = evt.tool_calls;
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, tool_calls: toolCalls } : m)),
          );
        } else if (evt.type === 'activity_step' && evt.step && typeof evt.step.number === 'number') {
          const existingIdx = activitySteps.findIndex((s) => s.number === evt.step.number);
          if (existingIdx >= 0) {
            activitySteps[existingIdx] = { ...activitySteps[existingIdx], ...evt.step };
          } else {
            activitySteps.push(evt.step);
          }
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, activity_steps: [...activitySteps] } : m)),
          );
        } else if (evt.type === 'phase' && evt.verb) {
          currentPhase = { state: evt.state, verb: evt.verb, title: evt.title };
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, phase: currentPhase } : m)),
          );
        } else if (evt.type === 'artifact_created' && evt.artifact) {
          const art = evt.artifact;
          const artId = art.artifact_id || art.id;
          if (artId && !finalArtifacts.some((a) => (a.artifact_id || a.id) === artId)) {
            finalArtifacts = [...finalArtifacts, art];
          }
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, artifacts: [...finalArtifacts] } : m)),
          );
        } else if (evt.type === 'trace_step' && evt.step) {
          liveTraceSteps.push(evt.step);
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, trace_steps: [...liveTraceSteps] } : m)),
          );
        } else if (evt.type === 'reasoning_done' && typeof evt.reasoning === 'string') {
          if (evt.reasoning) reasoningText = evt.reasoning;
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, reasoning: reasoningText } : m)),
          );
        } else if (evt.type === 'plan' && evt.plan) {
          planSummary = evt.plan;
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, plan_summary: planSummary } : m)),
          );
        } else if (evt.type === 'error') {
          console.error('[EciscoBiWidget] stream error', evt.message || evt);
          fullContent = fullContent || '[Error generating response. Please try again.]';
          setMessages((prev) =>
            prev.map((m) => (m.id === aiMsgId ? { ...m, content: fullContent } : m)),
          );
        } else if (evt.type === 'done') {
          fullContent = typeof evt.content === 'string' ? evt.content : fullContent;

          const convMsgs = evt.conversation?.messages || [];
          const lastAssistant = [...convMsgs].reverse().find((m) => m?.role === 'assistant');
          if (Array.isArray(lastAssistant?.tool_calls) && lastAssistant.tool_calls.length) {
            toolCalls = lastAssistant.tool_calls;
          } else if (Array.isArray(evt.fsm_tool_calls) && evt.fsm_tool_calls.length) {
            toolCalls = evt.fsm_tool_calls;
          }
          if (Array.isArray(lastAssistant?.artifacts) && lastAssistant.artifacts.length) {
            finalArtifacts = lastAssistant.artifacts;
          }

          setMessages((prev) =>
            prev.map((m) =>
              m.id === aiMsgId
                ? {
                    ...m,
                    content: fullContent,
                    tool_calls: toolCalls,
                    isStreaming: false,
                    ...(finalArtifacts.length ? { artifacts: finalArtifacts } : {}),
                  }
                : m,
            ),
          );
          streamState.complete?.();
        }
      }

      // Defensive — if the stream ended without `done`, finalize anyway
      if (gen === genRef.current) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === aiMsgId ? { ...m, isStreaming: false } : m,
          ),
        );
        if (streamState.isActive) streamState.complete?.();
      }
    } catch (err) {
      if (err && (err.name === 'AbortError' || err.code === 20)) {
        if (gen === genRef.current) {
          setMessages((prev) =>
            prev.map((m) =>
              m.isStreaming ? { ...m, isStreaming: false } : m,
            ),
          );
        }
      } else {
        console.error('[EciscoBiWidget] send error', err);
        if (gen === genRef.current) {
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.isStreaming) {
              return prev.map((m) =>
                m.id === last.id
                  ? { ...m, content: m.content || '[Error generating response. Please try again.]', isStreaming: false }
                  : m,
              );
            }
            return [...prev, { id: uid(), role: 'assistant', content: '[Error generating response. Please try again.]' }];
          });
        }
      }
    } finally {
      if (gen === genRef.current) {
        setIsStreaming(false);
      }
    }
  }, [streamState, abortRef]);

  // ── Start a new conversation ───────────────────────────────────────
  const newChat = useCallback(() => {
    streamState.abort?.();
    genRef.current += 1;
    convIdRef.current = null;
    localStorage.removeItem(STORAGE_KEY);
    setMessages([]);
    setIsStreaming(false);
    setLoadStatus('idle');
  }, [streamState]);

  // ── Stop the current stream without clearing history ───────────────
  const stop = useCallback(() => {
    streamState.abort?.();
  }, [streamState]);

  // ── Cleanup on unmount ─────────────────────────────────────────────
  useEffect(() => {
    return () => {
      streamState.abort?.();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return {
    messages,
    isStreaming,
    loadStatus,
    send,
    newChat,
    stop,
  };
}
