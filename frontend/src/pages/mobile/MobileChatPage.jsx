import { useState, useEffect, useRef, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useChatSession } from '@/lib/ChatSessionContext';
import { toast } from 'sonner';
import ChatInput from '@/components/chat/ChatInput';
import MessageBubble from '@/components/chat/MessageBubble';
import MobileTopBar from '@/components/mobile/MobileTopBar';
import useStreamState from '@/hooks/useStreamState';
import { streamAgentResponse, createAgentConversation } from '@/api/agentEnhanced';
import { detectLang } from '@/lib/detectLang';
import { saveDraft, clearDraft } from '@/lib/draftManager';
import { usePersistentStream } from '@/lib/PersistentStreamContext';

/**
 * MobileChatPage — mobile-first chat (方案A).
 *
 * Deliberately simple: only the core conversation + input, NO desktop
 * side panels (Scheduled / Artifact / Preview / Dashboard). It reuses the
 * same building blocks as the desktop Chat.jsx — MessageBubble, ChatInput,
 * useChatSession, useStreamState, and the agentEnhanced streaming API —
 * but keeps the DOM to a single full-width column so it feels native on a
 * phone. The history is reached from the top-left corner (MobileTopBar →
 * HistorySheet) rather than a sidebar.
 */
export default function MobileChatPage() {
  const { t, lang, aiLang } = useLanguage();
  const {
    activeId,
    pendingProject,
    pendingProjectId,
    setActiveId,
    newChat,
    prependSession,
    touchSession,
    getSession,
    chatGeneration,
  } = useChatSession();

  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [inputValue, setInputValue] = useState('');
  const [activeAgent, setActiveAgent] = useState(null);
  const [activeSkill, setActiveSkill] = useState(null);
  const [attachments, setAttachments] = useState([]);
  const [streamingId, setStreamingId] = useState(null);
  const [streamingConvId, setStreamingConvId] = useState(null);
  const streamState = useStreamState();
  const persistent = usePersistentStream();
  const genRef = useRef(0);
  const streamingIdRef = useRef(null);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => { streamingIdRef.current = streamingId; }, [streamingId]);

  // Mirror the draft to localStorage so a typed-but-unsent message
  // survives a refresh.
  useEffect(() => {
    saveDraft('chat_input_draft', inputValue, 0);
  }, [inputValue]);

  // Load messages whenever the active session changes (or a new chat is
  // requested via chatGeneration).
  useEffect(() => {
    if (!activeId) {
      setMessages([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    base44.entities.ChatMessage.filter({ session_id: activeId }, 'created_date', 200)
      .then((msgs) => { if (!cancelled) setMessages(msgs || []); })
      .catch(() => { if (!cancelled) setMessages([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [activeId, chatGeneration]);

  // Auto-scroll to bottom when messages change.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  function scrollToBottom() {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }

  // Resume the agent conversation id for the current session, in priority
  // order: in-flight streamingConvId → session.conversation_id → URL ?conv=.
  const resumeConvId = useCallback((sid) => {
    if (streamingConvId) return streamingConvId;
    const sess = sid ? getSession(sid) : null;
    if (sess?.conversation_id) return sess.conversation_id;
    try {
      const urlConv = new URLSearchParams(window.location.search).get('conv');
      if (urlConv) return urlConv;
    } catch { /* SSR / test env */ }
    return null;
  }, [streamingConvId, getSession]);

  async function ensureSession(firstText) {
    if (activeId) return activeId;
    const session = await base44.entities.ChatSession.create({
      title: (firstText || t.chat.coreTitle).slice(0, 24),
      project: pendingProject || null,
      ...(pendingProjectId ? { project_id: pendingProjectId } : {}),
      last_message_at: new Date().toISOString(),
    });
    setActiveId(session.id);
    prependSession(session);
    return session.id;
  }

  // Auto-select general_assistant when no agent is chosen (same policy as
  // the desktop chat), so every conversation runs through the full agent
  // runtime (tools, memory, web search).
  async function resolveEffectiveAgent() {
    if (activeAgent) return activeAgent;
    try {
      const agents = await base44.entities.AgentApp.list();
      const ga = agents.find((a) => a.name === 'general_assistant') || null;
      return ga || { name: 'general_assistant' };
    } catch {
      return { name: 'general_assistant' };
    }
  }

  async function handleAgentSend(text, { activeAgent: agent, sid, gen, aiMsg, fileUrls, lang: outLang }) {
    const genNow = gen;
    let convId = resumeConvId(sid);
    if (!convId) {
      const conv = await createAgentConversation(
        agent.name,
        {
          description: agent.description || '',
          agent_app_id: agent.id,
          ...(pendingProjectId ? { project_id: pendingProjectId } : {}),
          ...(pendingProject ? { project: pendingProject } : {}),
        },
        text ? text.trim().replace(/\s+/g, ' ').slice(0, 60) : undefined,
      );
      convId = conv.id;
      if (sid) {
        try {
          await base44.entities.ChatSession.update(sid, {
            conversation_id: convId,
            agent_name: agent.name,
          });
        } catch { /* best-effort link */ }
      }
    }
    setStreamingConvId(convId);

    let fullContent = '';
    let toolCalls = [];
    let finalArtifacts = [];
    let reasoningText = '';
    let traceSteps = [];

    try {
      streamState.startStreaming();
      // Register this session as "running silently" in the shared registry
      // so the desktop sidebar can show a per-row spinner (concurrent-session
      // support). The mobile chat doesn't render the sidebar, but a session
      // started on mobile still shows as busy when viewed on desktop.
      persistent.setRunStatus(sid, 'running');
      const stream = streamAgentResponse(
        convId,
        {
          role: 'user',
          content: text,
          ...(fileUrls && fileUrls.length ? { file_urls: fileUrls } : {}),
          ...(outLang ? { lang: outLang } : {}),
        },
        streamState.abortRef.current?.signal,
      );
      for await (const evt of stream) {
        if (genNow !== genRef.current) {
          streamState.reset();
          return;
        }
        if (!evt || typeof evt !== 'object') continue;
        if (evt.type === 'delta' && typeof evt.content === 'string') {
          fullContent += evt.content;
          if (genNow === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, content: fullContent } : m
            ));
            scrollToBottom();
          }
        } else if (evt.type === 'content_replace' && typeof evt.content === 'string') {
          // D2 (2026-08-20): post-nudge re-synthesis. REPLACE the whole
          // bubble instead of appending so leaked nudge-reply prose never
          // shows; the `done` event carries the authoritative final content.
          fullContent = evt.content;
          if (genNow === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, content: fullContent } : m
            ));
            scrollToBottom();
          }
        } else if (evt.type === 'reasoning_done' && typeof evt.reasoning === 'string') {
          reasoningText = evt.reasoning;
          if (genNow === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, reasoning: reasoningText } : m
            ));
          }
        } else if (evt.type === 'trace_step' && evt.step) {
          traceSteps.push(evt.step);
          if (genNow === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, trace_steps: [...traceSteps] } : m
            ));
          }
        } else if (evt.type === 'error') {
          const friendlyErr = lang === 'en'
            ? "Sorry, I hit an error while responding. Please try again."
            : "抱歉，回复时出错了，请重试。";
          console.error('[Mobile stream error]', evt.message || evt);
          fullContent = fullContent || friendlyErr;
          if (genNow === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, content: fullContent } : m
            ));
          }
        } else if (evt.type === 'done') {
          // Defensive merge (2026-08-20): prefer the LONGER of
          // (locally-streamed deltas, server `done` content) so multi-iter
          // turns keep earlier iterations' prose in the visible bubble.
          if (typeof evt.content === 'string' && evt.content.length > (fullContent?.length || 0)) {
            fullContent = evt.content;
          }
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
          if (genNow === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id
                ? {
                    ...m,
                    content: fullContent,
                    tool_calls: toolCalls,
                    ...(finalArtifacts.length ? { artifacts: finalArtifacts } : {}),
                  }
                : m
            ));
          }
          if (genNow === genRef.current) streamState.complete();
        }
      }
      if (genNow === genRef.current && streamState.isActive) {
        streamState.complete();
        persistent.clearRunStatus(sid);
      }
    } catch (streamErr) {
      if (streamErr && (streamErr.name === 'AbortError' || streamErr.code === 20)) {
        aborted = true;
        if (genNow === genRef.current) {
          const stoppedText = lang === 'en' ? '\n\n[Stopped generating.]' : '\n\n[已停止生成。]';
          setMessages((prev) => prev.map((m) =>
            m.id === aiMsg.id ? { ...m, content: (m.content || '') + stoppedText } : m
          ));
        }
      } else {
        console.error('[Mobile stream error]', streamErr);
        const friendlyErr = lang === 'en'
          ? "Sorry, the connection was interrupted. Please try again."
          : "抱歉，连接中断了，请重试。";
        fullContent = fullContent || friendlyErr;
        if (genNow === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, content: fullContent } : m
            ));
            streamState.error();
            persistent.setRunStatus(sid, 'error');
          }
      }
    }
    // Persist the final assistant message + tool_calls + artifacts.
    try {
      await base44.entities.ChatMessage.update(aiMsg.id, {
        content: fullContent,
        ...(toolCalls.length ? { tool_calls: toolCalls } : {}),
        ...(finalArtifacts.length ? { artifacts: finalArtifacts } : {}),
      });
      await base44.entities.ChatSession.update(sid, { last_message_at: new Date().toISOString() });
      touchSession(sid);
    } catch { /* best-effort persist */ }
    setStreamingConvId((cur) => (cur === convId ? null : cur));
  }

  async function handleSend(text) {
    const fullText = (text || '').trim();
    if (!fullText || streamState.isActive) return;
    const gen = ++genRef.current;
    const sid = await ensureSession(fullText);
    const current = messages;
    const userMsg = await base44.entities.ChatMessage.create({
      session_id: sid, role: 'user', content: fullText, order: current.length,
      // Persist the attached files on the message so the file cards render
      // in chat history (Kimi/ChatGPT-style) and survive a page refresh.
      ...(attachments.length ? { attachments: attachments.map((a) => ({ name: a.name, file_url: a.file_url, ext: (a.name || '').split('.').pop()?.toLowerCase() || '', kind: 'document' })) } : {}),
    });
    const next = [...current, userMsg];
    setMessages(next);
    setInputValue('');
    clearDraft('chat_input_draft');
    setLoading(true);
    streamState.startSending();

    const effectiveAgent = await resolveEffectiveAgent();
    // Create the assistant placeholder bubble.
    const aiMsg = await base44.entities.ChatMessage.create({
      session_id: sid, role: 'assistant', content: '', order: next.length,
    });
    if (gen !== genRef.current) {
      streamState.reset();
      return;
    }
    setMessages((prev) => [...prev, aiMsg]);
    setStreamingId(aiMsg.id);
    setLoading(false);
    try {
      const effectiveAiLang = aiLang === 'auto' ? (detectLang(fullText) || 'zh') : aiLang;
      await handleAgentSend(fullText, {
        activeAgent: effectiveAgent,
        sid,
        gen,
        aiMsg,
        fileUrls: attachments.map((a) => a.file_url),
        lang: effectiveAiLang,
      });
    } finally {
      if (gen === genRef.current) {
        setLoading(false);
        setStreamingId(null);
        if (streamState.isActive) streamState.complete();
      }
    }
  }

  function handleNewChat() {
    genRef.current++;
    setStreamingId(null);
    setStreamingConvId(null);
    setMessages([]);
    setAttachments([]);
    setActiveAgent(null);
    setActiveSkill(null);
    setInputValue('');
    newChat(null);
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('conv');
      window.history.replaceState({}, '', url.toString());
    } catch { /* SSR / test env */ }
  }

  async function handleUploadFile(file) {
    try {
      const { file_url } = await base44.integrations.Core.UploadFile({ file });
      setAttachments((prev) => [...prev, { name: file.name, file_url }]);
    } catch (e) {
      toast.error(e?.message || 'Upload failed');
    }
  }

  function handleRemoveAttachment(idx) {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  }

  const showEmpty = messages.length === 0 && !loading;

  return (
    <div className="flex h-full flex-col bg-background">
      <MobileTopBar title={t.chat.coreTitle} showNewChat onNewChat={handleNewChat} />

      {/* Message list */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-3xl px-3 py-4">
          {showEmpty ? (
            <div className="flex flex-col items-center justify-center px-4 py-16 text-center">
              <h2 className="font-display text-xl text-foreground">{t.mobile.emptyChat}</h2>
              <p className="mt-2 text-sm text-muted-foreground">{t.mobile.emptyChatHint}</p>
            </div>
          ) : (
            messages.filter((m) => !m.hidden).map((m) => (
              <MessageBubble
                key={m.id}
                message={{ ...m, content: m.content || '' }}
                isStreaming={streamingId === m.id}
                onSelectOption={handleSend}
                onSelectOther={() => inputRef.current?.focus()}
              />
            ))
          )}
          {/* 2026-08-31: removed ChatThinkingIndicator; MessageBubble renders
              LiveActivityStream (with a rich placeholder while no events have
              arrived) so the mobile chat matches the desktop Fabricating card. */}
        </div>
      </div>

      {/* Composer */}
      <ChatInput
        value={inputValue}
        onChange={setInputValue}
        onSend={handleSend}
        onSelectSkill={(s) => setActiveSkill(s)}
        onSelectAgent={(a) => setActiveAgent(a)}
        onSelectProject={() => {}}
        onRemoveSkill={() => setActiveSkill(null)}
        onRemoveAgent={() => setActiveAgent(null)}
        activeSkill={activeSkill}
        activeAgent={activeAgent}
        disabled={loading}
        isStreaming={streamState.isActive}
        onStop={streamState.abort}
        inputRef={inputRef}
        onUploadFile={handleUploadFile}
        attachments={attachments}
        onRemoveAttachment={handleRemoveAttachment}
        pendingProject={pendingProject}
        onClearProject={() => {}}
      />
    </div>
  );
}
