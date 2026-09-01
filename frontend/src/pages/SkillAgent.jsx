import { useState, useEffect, useRef, useMemo } from 'react';
import { base44 } from '@/api/base44Client';
import { setPermissionMode, getPermissionMode, streamAgentResponse } from '@/api/agentEnhanced';
import { useLanguage } from '@/lib/LanguageProvider';
import SkillMessageBubble from '@/components/skillagent/SkillMessageBubble';
import SkillUploadButton from '@/components/skillagent/SkillUploadButton';
import SkillFilePanel from '@/components/skillagent/SkillFilePanel';
import SkillDraftPanel from '@/components/skillagent/SkillDraftPanel';
import ExistingSkillsChips from '@/components/skillagent/ExistingSkillsChips';
import PrefilledHintPill from '@/components/common/PrefilledHintPill';
import ChatThinkingIndicator from '@/components/chat/ChatThinkingIndicator';
import ArtifactPreviewSheet from '@/components/chat/ArtifactPreviewSheet';
import { Wrench, Send, Loader2, Github, Sparkles, Pencil, X, PanelRight, FileText, Shield, ShieldAlert, ShieldCheck, Globe } from 'lucide-react';
import ScrapeUrlDialog from '@/components/skillagent/ScrapeUrlDialog';

const AGENT_NAME = 'skill_agent';
const SUGGESTIONS = [
  { key: 'collect', icon: Github },
  { key: 'create', icon: Sparkles },
  { key: 'learn', icon: FileText },
  { key: 'edit', icon: Pencil },
];

export default function SkillAgent() {
  const { t } = useLanguage();
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [activeConv, setActiveConv] = useState(null);
  const [permissionMode, setPermissionModeState] = useState('default');
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [initializing, setInitializing] = useState(false);
  const [pendingFiles, setPendingFiles] = useState([]);
  const [currentSkill, setCurrentSkill] = useState(null);
  const [skillDraft, setSkillDraft] = useState(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const [openArtifact, setOpenArtifact] = useState(null);
  const [prefilledHint, setPrefilledHint] = useState(false);
  const [scrapeOpen, setScrapeOpen] = useState(false);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const pollRef = useRef(null);
  const completionRef = useRef(null);
  const abortRef = useRef(null);
  const streamingMsgIdRef = useRef(null);

  useEffect(() => { loadConversations(); }, []);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const skillId = params.get('skill');
    const action = params.get('action');
    const filesParam = params.get('files');
    if (filesParam) {
      const urls = filesParam.split(',').map(decodeURIComponent).filter(Boolean);
      startWithFiles(urls);
    } else if (skillId) {
      startWithSkill(skillId);
    } else if (action === 'create') {
      applySuggestion(t.skillAgent.suggestions.create);
    }
    window.history.replaceState({}, '', '/skill-agent');
  }, []);
  function handleIncomingMessages(msgs) {
    setMessages(msgs);
    if (completionRef.current) clearTimeout(completionRef.current);
    if (checkAgentDone(msgs)) {
      completionRef.current = setTimeout(() => {
        stopPolling();
        setLoading(false);
      }, 700);
    }
  }

  function checkAgentDone(msgs) {
    if (!msgs || msgs.length === 0) return false;
    const last = msgs[msgs.length - 1];
    if (last.role !== 'assistant') return false;
    const toolCalls = last.tool_calls || [];
    const hasPending = toolCalls.some((tc) =>
      ['pending', 'running', 'in_progress', 'awaiting_approval'].includes((tc.status || '').toLowerCase())
    );
    return !hasPending;
  }

  function startPolling(conv) {
    stopPolling();
    const startTime = Date.now();
    pollRef.current = setInterval(async () => {
      if (Date.now() - startTime > 180000) { stopPolling(); setLoading(false); return; }
      try {
        const fetched = await base44.agents.getConversation(conv.id);
        handleIncomingMessages(fetched.messages || []);
      } catch { /* noop */ }
    }, 1000);
  }

  function stopPolling() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    if (completionRef.current) { clearTimeout(completionRef.current); completionRef.current = null; }
  }

  useEffect(() => {
    if (!activeId) { setMessages([]); setActiveConv(null); setSkillDraft(null); return; }
    let unsub = () => { };
    (async () => {
      try {
        const conv = await base44.agents.getConversation(activeId);
        setActiveConv(conv);
        setPermissionModeState(getPermissionMode(conv));
        handleIncomingMessages(conv.messages || []);
        unsub = base44.agents.subscribeToConversation(activeId, (data) => {
          handleIncomingMessages(data.messages || []);
        });
      } catch { setMessages([]); }
    })();
    return () => { unsub(); stopPolling(); };
  }, [activeId]);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, loading]);

  // When the agent creates or updates a skill via tool calls, the file
  // panel must show the new skill's files. Scan assistant messages in
  // reverse chronological order and pull the first valid
  // create_skill / update_skill result into currentSkill.
  // See user bug report 2026-07-28: "skills created but it not
  // showing on the right side".
  useEffect(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const tcs = messages[i].tool_calls || [];
      for (const tc of tcs) {
        const status = (tc.status || '').toLowerCase();
        const isComplete = status === 'completed' || status === 'success';
        // Tool names can arrive in two formats: the LLM-facing name
        // (create_skill, update_skill) or the wire-format display name
        // (Tool.create, Tool.update). The backend's TOOL_DISPLAY_NAMES
        // map at backend/app/routers/agents.py:1124-1128 translates
        // between them. We accept both so a freshly created skill
        // lights up the file panel whether the stream is showing the
        // raw LLM name or the routed display name.
        const tcName = String(tc.name || '');
        const isSkillOp =
          tcName === 'create_skill' || tcName === 'Tool.create' ||
          tcName === 'update_skill' || tcName === 'Tool.update';
        if (!isComplete || !isSkillOp) continue;
        let res = tc.results;
        if (typeof res === 'string') {
          try { res = JSON.parse(res); } catch { continue; }
        }
        if (res && res.id) {
          setCurrentSkill(res);
          return;
        }
      }
    }
  }, [messages]);

  // Reconstruct the in-flight SkillDraft from the most recent assistant
  // message that carries a ``skill_draft`` payload (emitted by the creation
  // orchestrator). This keeps the live folder tree correct after a page
  // reload or when the SSE stream fell back to polling.
  useEffect(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const draft = messages[i].skill_draft;
      if (draft && typeof draft === 'object') {
        setSkillDraft(draft);
        return;
      }
    }
    setSkillDraft(null);
  }, [messages]);

  async function loadConversations() {
    try {
      const list = await base44.agents.listConversations({ agent_name: AGENT_NAME });
      setConversations(list || []);
    } catch { setConversations([]); }
  }

  async function startNewChat(prefillText, opts = {}) {
    setInitializing(true);
    setCurrentSkill(null);
    setSkillDraft(null);
    try {
      const conv = await base44.agents.createConversation({
        agent_name: AGENT_NAME,
        metadata: { name: prefillText ? prefillText.slice(0, 30) : t.skillAgent.untitled, description: '' },
      });
      setConversations((prev) => [conv, ...prev]);
      setActiveId(conv.id);
      setPanelOpen(true);
      if (prefillText) {
        setInput(prefillText);
        setPrefilledHint(false);
        if (!opts.deferSend) setTimeout(() => handleSend(prefillText, conv), 100);
      }
    } finally { setInitializing(false); }
  }

  function applySuggestion(text) {
    setInput(text);
    setPrefilledHint(true);
    inputRef.current?.focus();
  }

  async function startWithSkill(skillId) {
    setInitializing(true);
    try {
      const skill = await base44.entities.Tool.get(skillId);
      setCurrentSkill(skill);
      const conv = await base44.agents.createConversation({
        agent_name: AGENT_NAME,
        metadata: { name: `${t.skillAgent.editing}: ${skill.name}`, description: skill.description || '' },
      });
      setConversations((prev) => [conv, ...prev]);
      setActiveId(conv.id);
      setPanelOpen(true);
      const prompt = `${t.skillAgent.editPrefill} "${skill.name}". ${t.skillAgent.editPrefillDesc}`;
      setTimeout(() => handleSend(prompt, conv, skill), 100);
    } catch {
      setInitializing(false);
    }
  }

  async function startWithFiles(urls) {
    setInitializing(true);
    setCurrentSkill(null);
    try {
      const conv = await base44.agents.createConversation({
        agent_name: AGENT_NAME,
        metadata: { name: t.skillAgent.processFiles, description: '' },
      });
      setConversations((prev) => [conv, ...prev]);
      setActiveId(conv.id);
      setPanelOpen(true);
      setTimeout(() => handleSend(t.skillAgent.processFilesPrompt, conv, null, urls), 100);
    } finally { setInitializing(false); }
  }

  function handleFilesUploaded(files) {
    setPendingFiles((prev) => [...prev, ...files]);
  }

  function removePendingFile(idx) {
    setPendingFiles((prev) => prev.filter((_, i) => i !== idx));
  }

  async function handleSend(text, convArg, skillCtx, fileUrls) {
    const conv = convArg || conversations.find((c) => c.id === activeId);
    if (!conv) return;
    const content = text.trim();
    const urls = fileUrls || pendingFiles.map((f) => f.url);
    if (!content && urls.length === 0) return;
    setInput('');
    setPrefilledHint(false);
    setPendingFiles([]);
    setLoading(true);
    // Optimistic UI: render the user's message immediately (before the agent
    // starts responding), plus an empty assistant stub that will be filled in
    // by SSE delta / tool_progress events. This mirrors the general_assistant
    // Chat flow where the user bubble appears right away instead of only after
    // the agent finishes.
    const userMsgId = `user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const assistantMsgId = `assistant-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    streamingMsgIdRef.current = assistantMsgId;
    const optimisticUser = { id: userMsgId, role: 'user', content: content || t.skillAgent.attachedFiles.replace('{n}', urls.length) };
    if (urls.length > 0) optimisticUser.file_urls = urls;
    setMessages((prev) => [...prev, optimisticUser, { id: assistantMsgId, role: 'assistant', content: '' }]);
    let skillContext = '';
    if (skillCtx) {
      skillContext = `\n\n当前编辑的技能详情：\n名称: ${skillCtx.name}\n描述: ${skillCtx.description || ''}\n触发词: ${skillCtx.trigger || ''}\n分类: ${skillCtx.category || ''}\n版本: ${skillCtx.version || ''}\n许可证: ${skillCtx.license || ''}\n来源: ${(skillCtx.sources || []).join(', ')}\nSKILL.md 正文:\n${(skillCtx.skill_md || '').slice(0, 2000)}`;
    }
    const msg = { role: 'user', content: (content || t.skillAgent.attachedFiles.replace('{n}', urls.length)) + skillContext };
    if (urls.length > 0) msg.file_urls = urls;

    // Try SSE streaming first; fall back to polling if it fails.
    try {
      const controller = new AbortController();
      abortRef.current = controller;
      let usedSSE = false;
      // P0: accumulate trace_step and reasoning_done events
      let liveTraceSteps = [];
      let reasoningText = '';
      for await (const event of streamAgentResponse(conv.id, msg, controller.signal)) {
        usedSSE = true;
        if (event.type === 'delta') {
          setMessages((prev) => prev.map((m) =>
            m.id === streamingMsgIdRef.current ? { ...m, content: (m.content || '') + event.content } : m
          ));
        } else if (event.type === 'tool_progress') {
          // Update messages to show tool execution progress
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
          // P0: surface reasoning (keep last non-empty across iterations)
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
        } else if (event.type === 'done') {
          streamingMsgIdRef.current = null;
          // Live folder tree: the creation orchestrator attaches the latest
          // SkillDraft to the done event. ``skill_draft: null`` means the draft
          // was discarded or already saved, so clear the panel.
          if ('skill_draft' in event) {
            setSkillDraft(event.skill_draft || null);
            if (event.skill_draft && event.skill_draft.status === 'saved') {
              setCurrentSkill((prev) => prev || {
                name: event.skill_draft.name,
                description: event.skill_draft.description || '',
                skill_md: event.skill_draft.skill_md || '',
                category: event.skill_draft.category || 'custom',
              });
            }
          }
          if (event.conversation) {
            handleIncomingMessages(event.conversation.messages || []);
            setActiveConv(event.conversation);
          }
          setLoading(false);
          break;
        } else if (event.type === 'paused') {
          streamingMsgIdRef.current = null;
          if ('skill_draft' in event) {
            setSkillDraft(event.skill_draft || null);
          }
          if (event.conversation) {
            handleIncomingMessages(event.conversation.messages || []);
            setActiveConv(event.conversation);
          }
          setLoading(false);
          break;
        } else if (event.type === 'error') {
          console.error('SSE error:', event.message);
          break;
        }
      }
      if (!usedSSE) throw new Error('No SSE events received');
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.warn('SSE streaming failed, falling back to polling:', e);
      streamingMsgIdRef.current = null;
      // Fallback: use the standard v2 endpoint + polling
      try {
        await base44.agents.addMessage(conv, msg);
        startPolling(conv);
      } catch (e2) {
        console.error(e2);
        setLoading(false);
      }
    }
    inputRef.current?.focus();
  }

  async function handlePermissionModeChange(mode) {
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
  }

  async function handleDeleteConv(id) {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (activeId === id) { setActiveId(null); setMessages([]); setCurrentSkill(null); setSkillDraft(null); }
    try { await base44.agents.updateConversation(id, { metadata: { _deleted: true } }); } catch { /* noop */ }
    loadConversations();
  }

  const showEmpty = !activeId && messages.length === 0;

  // The assistant stub we optimistically inserted is "empty" until the first
  // delta / tool_progress event arrives. While it's empty, render the thinking
  // indicator instead of a blank assistant bubble.
  const streamingStub = messages.find((m) => m.id === streamingMsgIdRef.current);
  const streamingEmpty = loading && !!streamingStub && streamingStub.role === 'assistant' && !streamingStub.content && !(streamingStub.tool_calls || []).length && !streamingStub.reasoning && !(streamingStub.trace_steps || []).length;

  const renderInputArea = (isCompact) => (
    <div className="rounded-xl border border-border bg-card shadow-sm transition focus-within:ring-2 focus-within:ring-primary/30">
      {pendingFiles.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-3 pt-2">
          {pendingFiles.map((f, i) => (
            <span key={i} className="inline-flex items-center gap-1 rounded-md bg-secondary px-2 py-0.5 text-[11px] text-muted-foreground">
              <span className="max-w-[160px] truncate">{f.name}</span>
              <button onClick={() => removePendingFile(i)} className="text-muted-foreground hover:text-destructive"><X className="h-3 w-3" /></button>
            </span>
          ))}
        </div>
      )}
      {prefilledHint && (
        <PrefilledHintPill label={t.skillAgent.prefilledHint} onDismiss={() => setPrefilledHint(false)} />
      )}
      <textarea
        ref={inputRef}
        value={input}
        onChange={(e) => { setInput(e.target.value); if (prefilledHint) setPrefilledHint(false); }}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); showEmpty ? startNewChat(input) : handleSend(input); } }}
        placeholder={t.skillAgent.placeholder}
        rows={2}
        disabled={loading}
        className="w-full resize-none bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
      />
      <div className="flex items-center justify-between px-3 pb-2">
        <SkillUploadButton onUploaded={handleFilesUploaded} disabled={loading} />
        <button onClick={() => showEmpty ? startNewChat(input) : handleSend(input)} disabled={loading || (!input.trim() && pendingFiles.length === 0)} className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity disabled:opacity-40">
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />} {t.common.send}
        </button>
      </div>
    </div>
  );

  return (
    <div className="flex h-full">
      <div className="relative flex flex-1 overflow-hidden">
        {/* Chat pane */}
        <div className="flex min-w-0 flex-1 flex-col">
          {showEmpty ? (
            <div className="flex flex-1 flex-col items-center justify-center px-6 pb-12">
              <div className="mb-6 flex w-full max-w-2xl items-center gap-2">
                <Wrench className="h-5 w-5 text-primary" />
                <h1 className="font-display text-2xl tracking-tight text-foreground">{t.skillAgent.title}</h1>
                <span className="ml-1 text-sm text-muted-foreground">{t.skillAgent.subtitle}</span>
              </div>
              <div className="w-full max-w-2xl">
                {renderInputArea(true)}
              </div>
              <div className="mt-6 w-full max-w-2xl">
                <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                  {t.skillAgent.tryOne}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {[
                    { key: 'scrape', text: t.skillAgent.scrape.chip, icon: Globe, action: 'scrape' },
                    { key: 'weekly', text: t.skillAgent.chips.weekly },
                    { key: 'pdf', text: t.skillAgent.chips.pdf },
                    { key: 'code', text: t.skillAgent.chips.code },
                    { key: 'import', text: t.skillAgent.chips.import },
                  ].map((chip) => (
                    <button
                      key={chip.key}
                      type="button"
                      onClick={() => chip.action === 'scrape' ? setScrapeOpen(true) : applySuggestion(chip.text)}
                      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-foreground transition hover:border-primary/40 hover:bg-secondary/60"
                    >
                      {chip.icon && <chip.icon className="h-3.5 w-3.5 text-primary" />}
                      {chip.text}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between border-b border-border px-4 py-2">
                <div className="flex items-center gap-3">
                  <span className="text-xs text-muted-foreground">{currentSkill ? `${t.skillAgent.editing}: ${currentSkill.name}` : t.skillAgent.title}</span>
                  <div className="flex items-center gap-1">
                    {permissionMode === 'default' && <Shield className="h-3 w-3 text-muted-foreground" />}
                    {permissionMode === 'plan' && <ShieldAlert className="h-3 w-3 text-amber-500" />}
                    {permissionMode === 'full_auto' && <ShieldCheck className="h-3 w-3 text-green-500" />}
                    <select
                      value={permissionMode}
                      onChange={(e) => handlePermissionModeChange(e.target.value)}
                      className="bg-transparent text-[11px] text-muted-foreground border-none cursor-pointer hover:text-foreground focus:outline-none"
                      title="Permission mode"
                    >
                      <option value="default">Default</option>
                      <option value="plan">Plan</option>
                      <option value="full_auto">Full Auto</option>
                    </select>
                  </div>
                </div>
                <button onClick={() => setPanelOpen(!panelOpen)} className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground">
                  <PanelRight className="h-4 w-4" />
                </button>
              </div>
              <div ref={scrollRef} className="flex-1 space-y-6 overflow-y-auto px-6 py-6">
                <div className="mx-auto max-w-3xl space-y-6">
                  {messages.filter((m) => !(m.id === streamingMsgIdRef.current && streamingEmpty)).map((m, i) => <SkillMessageBubble key={i} message={m} conversationId={activeId} onArtifactPreview={setOpenArtifact} onOptionSelect={applySuggestion} />)}
                  {streamingEmpty && <ChatThinkingIndicator />}
                </div>
              </div>
              <div className="border-t border-border bg-card/50 px-6 py-4">
                <div className="mx-auto max-w-3xl">
                  {renderInputArea(false)}
                </div>
              </div>
            </>
          )}
        </div>

        {/* File panel: live draft tree during creation, else saved-skill files */}
        {activeId && panelOpen && (
          <>
            <div className="absolute inset-0 z-30 lg:static lg:z-auto lg:w-[42%] lg:shrink-0 lg:border-l lg:border-border">
              {skillDraft ? (
                <SkillDraftPanel
                  draft={skillDraft}
                  conversationId={activeId}
                  onClose={() => setPanelOpen(false)}
                  onDiscarded={() => setSkillDraft(null)}
                />
              ) : (
                <SkillFilePanel skill={currentSkill} messages={messages} onClose={() => setPanelOpen(false)} />
              )}
            </div>
            <div className="absolute inset-0 z-20 bg-black/30 lg:hidden" onClick={() => setPanelOpen(false)} />
          </>
        )}
      </div>
      <ArtifactPreviewSheet artifact={openArtifact} open={!!openArtifact} onOpenChange={(open) => { if (!open) setOpenArtifact(null); }} />
      <ScrapeUrlDialog
        open={scrapeOpen}
        onClose={() => setScrapeOpen(false)}
        onCollected={(result) => {
          // Set the collected skill as current so the file panel shows it
          if (result.skill_name) {
            setCurrentSkill({
              name: result.skill_name,
              description: result.description || '',
              skill_md: '',
              category: 'collected',
              source: 'collected',
            });
            setPanelOpen(true);
          }
        }}
      />
    </div>
  );
}