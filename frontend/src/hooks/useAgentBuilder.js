import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { base44 } from '@/api/base44Client';
import { setPermissionMode, getPermissionMode, streamAgentResponse } from '@/api/agentEnhanced';

const AGENT_NAME = 'agent_builder';

/**
 * Extract the agent id from a list of messages by scanning tool calls.
 * Accepts both the display name (agentapp) and canonical names
 * (create_agent / update_agent).
 */
export function extractAgentId(messages) {
  for (let i = (messages || []).length - 1; i >= 0; i--) {
    const toolCalls = messages[i].tool_calls || [];
    for (let j = toolCalls.length - 1; j >= 0; j--) {
      const tc = toolCalls[j];
      const tcName = (tc.name || '').toLowerCase();
      const matches =
        tcName.includes('agentapp') ||
        tcName === 'create_agent' ||
        tcName === 'update_agent' ||
        tcName.endsWith('.create_agent') ||
        tcName.endsWith('.update_agent');
      if (!matches) continue;
      let results = tc.results;
      if (typeof results === 'string') {
        try { results = JSON.parse(results); } catch { continue; }
      }
      if (!results || typeof results !== 'object') continue;
      // Skip the awaiting_approval stub — it has no real agent id
      if (results.requires_approval) continue;
      const obj = results.data || results;
      if (obj && (obj.id || obj.agent_id)) {
        return obj.id || obj.agent_id;
      }
    }
  }
  return null;
}

/**
 * Reusable hook that encapsulates all Agent Builder state and core logic:
 *   - conversations list, active conversation, messages
 *   - SSE streaming + polling fallback
 *   - agent-creation detection (extractAgentId + defensive scans)
 *   - decision-summary pause handling
 *
 * @param {object}   opts
 * @param {function} [opts.onAgentCreated]  Called with { id, name, skills } once when an agent is detected.
 * @param {boolean}  [opts.autoNavigateOnCreate] When true, the hook will not
 *   block navigation; consumers are responsible for reacting to onAgentCreated.
 * @param {string}   [opts.t]  Translations object (used for edit-prompt wording).
 */
export function useAgentBuilder({ onAgentCreated, t } = {}) {
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [activeConv, setActiveConv] = useState(null);
  const [permissionMode, setPermissionModeState] = useState('default');
  const [messages, setMessages] = useState([]);
  const [streamingContent, setStreamingContent] = useState('');
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [initializing, setInitializing] = useState(false);
  const [createdAgent, setCreatedAgent] = useState(null);
  const [preflightResult, setPreflightResult] = useState(null);
  const [decisionPayload, setDecisionPayload] = useState(null);
  const [openArtifact, setOpenArtifact] = useState(null);
  const [prefilledHint, setPrefilledHint] = useState(false);

  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const shownAgentIdRef = useRef(null);
  const pollRef = useRef(null);
  const completionRef = useRef(null);
  const abortRef = useRef(null);
  const onAgentCreatedRef = useRef(onAgentCreated);
  // Mirror of `streamingContent` that the SSE loop can read inside the
  // `done` / `paused` handler.
  const streamingContentRef = useRef('');

  useEffect(() => { onAgentCreatedRef.current = onAgentCreated; }, [onAgentCreated]);

  const loadConversations = useCallback(async () => {
    try {
      const list = await base44.agents.listConversations({ agent_name: AGENT_NAME });
      setConversations(list || []);
    } catch { setConversations([]); }
  }, []);

  useEffect(() => { loadConversations(); }, [loadConversations]);

  const checkAgentDone = useCallback((msgs) => {
    if (!msgs || msgs.length === 0) return false;
    const last = msgs[msgs.length - 1];
    if (last.role !== 'assistant') return false;
    const toolCalls = last.tool_calls || [];
    const hasPending = toolCalls.some((tc) =>
      ['pending', 'running', 'in_progress', 'awaiting_approval'].includes((tc.status || '').toLowerCase())
    );
    return !hasPending;
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    if (completionRef.current) { clearTimeout(completionRef.current); completionRef.current = null; }
  }, []);

  const handleIncomingMessages = useCallback((msgs) => {
    setMessages(msgs);
    if (completionRef.current) clearTimeout(completionRef.current);
    if (checkAgentDone(msgs)) {
      completionRef.current = setTimeout(() => {
        stopPolling();
        setLoading(false);
      }, 700);
    }
  }, [checkAgentDone, stopPolling]);

  const startPolling = useCallback((conv) => {
    stopPolling();
    const startTime = Date.now();
    pollRef.current = setInterval(async () => {
      if (Date.now() - startTime > 180000) { stopPolling(); setLoading(false); return; }
      try {
        const fetched = await base44.agents.getConversation(conv.id);
        handleIncomingMessages(fetched.messages || []);
      } catch { /* noop */ }
    }, 1000);
  }, [handleIncomingMessages, stopPolling]);

  useEffect(() => {
    if (!activeId) { setMessages([]); setActiveConv(null); setDecisionPayload(null); return; }
    let unsub = () => {};
    (async () => {
      try {
        const conv = await base44.agents.getConversation(activeId);
        setActiveConv(conv);
        setPermissionModeState(getPermissionMode(conv));
        const msgs = conv.messages || [];
        handleIncomingMessages(msgs);
        const meta = (conv && conv.metadata) || {};
        if (meta.awaiting_decision_summary && meta.pending_agent_payload) {
          setDecisionPayload(meta.pending_agent_payload);
        } else {
          setDecisionPayload(null);
        }
        unsub = base44.agents.subscribeToConversation(activeId, (data) => {
          handleIncomingMessages(data.messages || []);
        });
      } catch { setMessages([]); }
    })();
    return () => { unsub(); stopPolling(); };
  }, [activeId, handleIncomingMessages, stopPolling]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, loading]);

  const agentId = useMemo(() => extractAgentId(messages), [messages]);

  // Centralized "agent created" detection. Fires the onAgentCreated
  // callback exactly once per agent id.
  const fireAgentCreated = useCallback((agentInfo) => {
    if (!agentInfo || !agentInfo.id) return;
    if (shownAgentIdRef.current === agentInfo.id) return;
    shownAgentIdRef.current = agentInfo.id;
    setCreatedAgent(agentInfo);
    if (typeof onAgentCreatedRef.current === 'function') {
      onAgentCreatedRef.current(agentInfo);
    }
  }, []);

  useEffect(() => {
    if (agentId && agentId !== shownAgentIdRef.current) {
      queueMicrotask(() => {
        base44.entities.AgentApp.get(agentId)
          .then((a) => fireAgentCreated({ id: agentId, name: a.name, skills: a.skills || [] }))
          .catch(() => fireAgentCreated({ id: agentId, name: '', skills: [] }));
      });
    }
  }, [agentId, messages.length, fireAgentCreated]);

  // Fetch preflight result when an agent is created
  useEffect(() => {
    if (createdAgent && createdAgent.id) {
      setPreflightResult(null);
      fetch(`/api/agent-studio/${createdAgent.id}/preflight`)
        .then((r) => r.json())
        .then((data) => setPreflightResult(data))
        .catch(() => setPreflightResult(null));
    } else {
      setPreflightResult(null);
    }
  }, [createdAgent]);

  // Defensive: scan the latest assistant message for a successful
  // create_agent / update_agent tool call, even when extractAgentId() missed it.
  useEffect(() => {
    if (createdAgent) return;
    if (loading) return;
    if (agentId && agentId !== shownAgentIdRef.current) {
      queueMicrotask(() => {
        base44.entities.AgentApp.get(agentId)
          .then((a) => fireAgentCreated({ id: agentId, name: a.name, skills: a.skills || [] }))
          .catch(() => fireAgentCreated({ id: agentId, name: '', skills: [] }));
      });
    }
  }, [loading, messages.length, agentId, createdAgent, fireAgentCreated]);

  // Final guarantee: scan the latest assistant message for a successful
  // create_agent / update_agent tool call result.
  useEffect(() => {
    if (createdAgent) return;
    if (!messages.length) return;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role !== 'assistant') continue;
      const tcs = m.tool_calls || [];
      for (let j = tcs.length - 1; j >= 0; j--) {
        const tc = tcs[j];
        const name = String(tc.name || '').toLowerCase();
        const isMutation = name === 'create_agent' || name === 'update_agent'
          || name.includes('agentapp.create') || name.includes('agentapp.update');
        if (!isMutation) continue;
        const status = String(tc.status || '').toLowerCase();
        const isSuccess = status === 'completed' || status === 'success';
        if (!isSuccess) continue;
        let results = tc.results;
        if (typeof results === 'string') {
          try { results = JSON.parse(results); } catch { results = null; }
        }
        const data = (results && typeof results === 'object') ? (results.data || results) : null;
        const newId = (data && (data.id || data.agent_id)) || `tool-${tc.id || j}`;
        if (shownAgentIdRef.current === newId) return;
        fireAgentCreated({
          id: data?.id || data?.agent_id || null,
          name: data?.name || '',
          skills: Array.isArray(data?.skills) ? data.skills : [],
        });
        return;
      }
      break;
    }
  }, [messages, createdAgent, fireAgentCreated]);

  const handleSend = useCallback(async (text, convArg, agentCtx) => {
    const conv = convArg || conversations.find((c) => c.id === activeId);
    if (!conv) return;
    const content = text.trim();
    if (!content) return;
    setInput('');
    setPrefilledHint(false);
    setLoading(true);
    setStreamingContent('');
    streamingContentRef.current = '';
    // Append the user message to local state immediately so the user
    // always sees their own question — even if the stream errors out.
    const userMsgId = `user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setMessages((prev) => [...prev, { id: userMsgId, role: 'user', content }]);
    let agentContext = '';
    if (agentCtx) {
      agentContext = `\n\nCurrent agent configuration:\nName: ${agentCtx.name}\nDescription: ${agentCtx.description || ''}\nProject: ${agentCtx.project || 'global'}\nModel: ${agentCtx.model || 'automatic'}\nL1 Identity: ${agentCtx.prompt_identity || ''}\nL2 Constraint: ${agentCtx.prompt_boundary || ''}\nL3 Process: ${agentCtx.prompt_reasoning || ''}\nL4 Tools: ${agentCtx.prompt_tools || ''}\nL5 Output: ${agentCtx.prompt_output || ''}\nCapabilities: ${(agentCtx.capabilities || []).join(', ')}\nSkills: ${(agentCtx.skills || []).join(', ')}`;
    }
    const msg = { role: 'user', content: content + agentContext };

    try {
      const controller = new AbortController();
      abortRef.current = controller;
      let usedSSE = false;
      // P0: accumulate trace_step + reasoning_done for the builder preview
      const liveTraceSteps = [];
      let reasoningText = '';
      for await (const event of streamAgentResponse(conv.id, msg, controller.signal)) {
        usedSSE = true;
        if (event.type === 'delta') {
          setStreamingContent((prev) => {
            const next = prev + event.content;
            streamingContentRef.current = next;
            return next;
          });
        } else if (event.type === 'tool_progress') {
          setMessages((prev) => {
            if (prev.length === 0) return prev;
            const updated = [...prev];
            const lastIdx = updated.length - 1;
            if (updated[lastIdx]?.role === 'assistant') {
              updated[lastIdx] = { ...updated[lastIdx], tool_calls: event.tool_calls };
            } else {
              updated.push({ id: 'streaming', role: 'assistant', content: '', tool_calls: event.tool_calls });
            }
            return updated;
          });
        } else if (event.type === 'trace_step' && event.step) {
          // P0: incremental trace_step
          liveTraceSteps.push(event.step);
          setMessages((prev) => {
            if (prev.length === 0) return prev;
            const updated = [...prev];
            const lastIdx = updated.length - 1;
            if (updated[lastIdx]?.role === 'assistant') {
              updated[lastIdx] = { ...updated[lastIdx], trace_steps: [...liveTraceSteps] };
            }
            return updated;
          });
        } else if (event.type === 'reasoning_done' && typeof event.reasoning === 'string') {
          // P0: surface reasoning
          if (event.reasoning) {
            reasoningText = event.reasoning;
            setMessages((prev) => {
              if (prev.length === 0) return prev;
              const updated = [...prev];
              const lastIdx = updated.length - 1;
              if (updated[lastIdx]?.role === 'assistant') {
                updated[lastIdx] = { ...updated[lastIdx], reasoning: reasoningText };
              }
              return updated;
            });
          }
        } else if (event.type === 'done' || event.type === 'paused') {
          const streamed = streamingContentRef.current;
          const newMessages = (event.conversation && event.conversation.messages) || [];
          setMessages((prev) => {
            let next = Array.isArray(newMessages) && newMessages.length > 0
              ? newMessages.slice()
              : prev;
            let lastIdx = -1;
            for (let i = next.length - 1; i >= 0; i--) {
              if (next[i] && next[i].role === 'assistant') { lastIdx = i; break; }
            }
            const last = lastIdx >= 0 ? next[lastIdx] : null;
            const lastIsEmptyAssistant = !!(last && (last.content || '').trim() === '');
            const lastIsUser = !!(next.length > 0 && next[next.length - 1].role === 'user');
            const needSyntheticAssistant = streamed && (
              next.length === 0
              || lastIsEmptyAssistant
              || lastIsUser
            );
            if (needSyntheticAssistant) {
              if (lastIsEmptyAssistant) {
                next = [...next.slice(0, lastIdx), { ...last, content: streamed }, ...next.slice(lastIdx + 1)];
              } else {
                next = [
                  ...next,
                  {
                    id: `streamed-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                    role: 'assistant',
                    content: streamed,
                  },
                ];
              }
            }
            if (completionRef.current) clearTimeout(completionRef.current);
            if (checkAgentDone(next)) {
              completionRef.current = setTimeout(() => {
                stopPolling();
                setLoading(false);
              }, 700);
            }
            return next;
          });
          if (event.conversation) setActiveConv(event.conversation);
          if (event.type === 'paused' && event.reason === 'awaiting_decision_summary') {
            const meta = (event.conversation && event.conversation.metadata) || {};
            if (meta.pending_agent_payload) {
              setDecisionPayload(meta.pending_agent_payload);
            }
          } else if (event.type === 'done') {
            setDecisionPayload(null);
          }
          setStreamingContent('');
          streamingContentRef.current = '';
          setLoading(false);
          break;
        } else if (event.type === 'error') {
          console.error('SSE error:', event.message);
          setStreamingContent('');
          streamingContentRef.current = '';
          if (event.conversation && Array.isArray(event.conversation.messages) && event.conversation.messages.length > 0) {
            handleIncomingMessages(event.conversation.messages);
            setActiveConv(event.conversation);
          } else {
            setMessages((prev) => [
              ...prev,
              {
                id: `err-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                role: 'assistant',
                content: `**[Error]** The agent run failed: ${event.message || 'unknown error'}. You can retry by sending another message.`,
              },
            ]);
          }
          setLoading(false);
          try {
            const fetched = await base44.agents.getConversation(conv.id);
            if (fetched && Array.isArray(fetched.messages) && fetched.messages.length > 0) {
              handleIncomingMessages(fetched.messages);
            }
          } catch { /* noop */ }
          break;
        }
      }
      if (!usedSSE) throw new Error('No SSE events received');
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.warn('SSE streaming failed, falling back to polling:', e);
      setStreamingContent('');
      try {
        await base44.agents.addMessage(conv, msg);
        startPolling(conv);
      } catch (e2) {
        console.error(e2);
        setLoading(false);
      }
    } finally {
      inputRef.current?.focus();
    }
  }, [conversations, activeId, checkAgentDone, stopPolling, handleIncomingMessages, startPolling]);

  const startNewChat = useCallback(async (promptText) => {
    if (!promptText || !promptText.trim()) return;
    setInitializing(true);
    try {
      const conv = await base44.agents.createConversation({
        agent_name: AGENT_NAME,
        metadata: { name: promptText.slice(0, 30), description: '' },
      });
      setConversations((prev) => [conv, ...prev]);
      setActiveId(conv.id);
      setTimeout(() => handleSend(promptText, conv), 100);
    } finally { setInitializing(false); }
  }, [handleSend]);

  const startWithEdit = useCallback(async (id) => {
    setInitializing(true);
    try {
      const agent = await base44.entities.AgentApp.get(id);
      const conv = await base44.agents.createConversation({
        agent_name: AGENT_NAME,
        metadata: { name: `Edit: ${agent.name}`, description: agent.description || '' },
      });
      setConversations((prev) => [conv, ...prev]);
      setActiveId(conv.id);
      const prompt = `Help me improve this agent: "${agent.name}". Show me the current configuration and help me modify it.`;
      setTimeout(() => handleSend(prompt, conv, agent), 100);
    } catch {
      setInitializing(false);
    }
  }, [handleSend]);

  const handlePermissionModeChange = useCallback(async (mode) => {
    if (!activeId) return;
    const prevMode = permissionMode;
    setPermissionModeState(mode);
    try {
      await setPermissionMode(activeId, mode);
      setActiveConv((prev) => prev ? { ...prev, metadata: { ...(prev.metadata || {}), permission_mode: mode } } : prev);
    } catch (e) {
      console.error('Failed to set permission mode:', e);
      setPermissionModeState(prevMode);
    }
  }, [activeId, permissionMode]);

  const handleDeleteConv = useCallback((id) => {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (activeId === id) { setActiveId(null); setMessages([]); }
    try { base44.agents.updateConversation(id, { metadata: { _deleted: true } }); } catch { /* noop */ }
    loadConversations();
  }, [activeId, loadConversations]);

  const handleSelectOption = useCallback((text) => {
    setInput(text);
    inputRef.current?.focus();
  }, []);

  const clearCreatedAgent = useCallback(() => setCreatedAgent(null), []);

  const applySuggestion = useCallback((text) => {
    setInput(text);
    setPrefilledHint(true);
    inputRef.current?.focus();
  }, []);
  // Allow consumers to programmatically set the created agent (e.g. after
  // confirming a decision-summary card). Dedupes via shownAgentIdRef.
  const setCreatedAgentValue = useCallback((agentInfo) => {
    fireAgentCreated(agentInfo);
  }, [fireAgentCreated]);

  return {
    // state
    conversations,
    activeId,
    activeConv,
    permissionMode,
    messages,
    streamingContent,
    input,
    loading,
    initializing,
    createdAgent,
    preflightResult,
    decisionPayload,
    openArtifact,
    // setters
    setActiveId,
    setInput,
    setOpenArtifact,
    prefilledHint,
    setPrefilledHint,
    applySuggestion,
    setMessages,
    setCreatedAgent: clearCreatedAgent,
    setCreatedAgentValue,
    setDecisionPayload,
    setLoading,
    // refs (for consumers that need scroll focus etc.)
    scrollRef,
    inputRef,
    // methods
    loadConversations,
    startNewChat,
    startWithEdit,
    handleSend,
    handleIncomingMessages,
    handlePermissionModeChange,
    handleDeleteConv,
    handleSelectOption,
  };
}
