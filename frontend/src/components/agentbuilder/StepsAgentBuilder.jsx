import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAgentBuilder } from '@/hooks/useAgentBuilder';
import { useLanguage } from '@/lib/LanguageProvider';
import { base44 } from '@/api/base44Client';
import { Button } from '@/components/ui/button';
import { Loader2, ArrowLeft, ArrowRight, Send, Bot, Check, Trash2, Edit3, MessageSquare } from 'lucide-react';
import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels';
import AgentCreateForm from '@/components/agentbuilder/AgentCreateForm';
import BuilderMessageBubble from '@/components/agentbuilder/BuilderMessageBubble';
import RoleSection from '@/components/agent/RoleSection';
import PromptSection from '@/components/agent/PromptSection';
import SkillsSection from '@/components/agent/SkillsSection';
import { saveDraft, loadDraft, clearDraft } from '@/lib/draftManager';

const DRAFT_KEY_FORM  = 'agent_steps_form';
const DRAFT_KEY_CONFIG = 'agent_steps_config';

// Normalize a free-form capabilities value (string with comma/semicolon
// separators, or already an array) into a clean string array. Used at the
// write boundary so the JSON column on AgentApp never receives a bare
// string like "数据分析, 报表生成" — downstream consumers
// (CapabilitiesSection.jsx, Chat.jsx …) treat this column strictly as
// an array of strings and would crash with TypeError on .map()/.filter()
// otherwise.
function parseCapabilities(value) {
  if (Array.isArray(value)) return value.map((s) => String(s).trim()).filter(Boolean);
  if (typeof value !== 'string') return [];
  return value
    .split(/[,，;；、/]/g)
    .map((s) => s.trim())
    .filter(Boolean);
}

// Standardized initial config matching AgentConfig defaults.
function emptyConfig() {
  return {
    name: '', description: '', model: 'automatic', project: 'global',
    prompt_identity: '', prompt_boundary: '', prompt_reasoning: '',
    prompt_tools: '', prompt_output: '', skills: [],
    capabilities: [], agent_type: 'sequential',
  };
}

/**
 * BuilderChatInput — minimal chat input mirroring the cognitive-hub
 * `ChatInput` look (rounded-xl border, focus-within ring). Used in both
 * the centered hero and the bottom-aligned chat bar.
 */
function BuilderChatInput({ input, onInputChange, onSend, onKeyDown, disabled, placeholder, inputRef }) {
  return (
    <div className="rounded-xl border border-border bg-card shadow-sm transition focus-within:ring-2 focus-within:ring-primary/30">
      <textarea
        ref={inputRef}
        value={input}
        onChange={(e) => onInputChange(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        rows={2}
        disabled={disabled}
        className="w-full resize-none bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
      />
      <div className="flex items-center justify-end px-3 pb-2">
        <Button
          size="icon"
          onClick={onSend}
          disabled={disabled || !input.trim()}
          className="shrink-0 h-8 w-8"
        >
          {disabled ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
        </Button>
      </div>
    </div>
  );
}

/**
 * StepsAgentBuilder – embedded 2-step Agent creation flow.
 *
 * Step 1 (form):    Name / desc / capabilities / project / templates → "Build"
 * Step 2 (config):  Left Chat (useAgentBuilder) + Right config preview
 *                    (RoleSection + PromptSection + SkillsSection).
 */
export default function StepsAgentBuilder({ onClose, initialProjectId }) {
  const { t } = useLanguage();
  const navigate = useNavigate();

  // ── Step state ──
  const [step, setStep] = useState('form'); // 'form' | 'config'

  // ── Step 1 form fields ──
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [capabilities, setCapabilities] = useState('');
  const [llmModelId, setLlmModelId] = useState(null);
  // `project` now holds the selected project *id* (the FK column on AgentApp).
  // Falls back to 'global' for "Ungrouped". The legacy `project` name string
  // is derived from project_id at save time and written alongside it for
  // backward compatibility with code that still reads the name field.
  const [project, setProject] = useState(initialProjectId || 'global');
  // Project list loaded once so we can resolve ids → names when writing
  // the legacy `project` field for backward compatibility.
  const [projects, setProjects] = useState([]);

  // ── Step 2 local config (right panel) ──
  const [localConfig, setLocalConfig] = useState(emptyConfig);
  const [dirtyFields, setDirtyFields] = useState(new Set());

  // ── Save / success state ──
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [savedAgentId, setSavedAgentId] = useState(null);
  // Tracks whether Step 2 was reached by clicking the Step 1 "Build"
  // button — in that case the prefill has already been submitted to a
  // fresh conversation, so the empty chat state should show a
  // chat-with-prefill layout instead of the centered hero (which would
  // otherwise flash for a moment and look like the chat is broken).
  const [enteredWithPrefill, setEnteredWithPrefill] = useState(false);

  // ── Draft prompt on mount ──
  const [draftDialog, setDraftDialog] = useState(false);
  const draftChecked = useRef(false);

  // ── Builder hook ──
  const {
    messages, streamingContent, input, loading, initializing,
    decisionPayload, createdAgent,
    setInput, setMessages,
    startNewChat, handleSend,
    handleSelectOption,
    scrollRef, inputRef,
  } = useAgentBuilder({ t });

  // ── Detect agent created via tool calls ──
  useEffect(() => {
    if (createdAgent && createdAgent.id) {
      setSavedAgentId(createdAgent.id);
      // Auto-sync created agent info into localConfig name/skills if unchanged
      setLocalConfig((prev) => {
        const next = { ...prev };
        if (createdAgent.name && (!dirtyFields.has('name') || !prev.name)) {
          next.name = createdAgent.name;
        }
        if (Array.isArray(createdAgent.skills) && createdAgent.skills.length > 0) {
          if (!dirtyFields.has('skills') || prev.skills.length === 0) {
            next.skills = createdAgent.skills;
          }
        }
        return next;
      });
    }
  }, [createdAgent]);

  // ── Smart merge: decisionPayload → localConfig ──
  const prevPayloadRef = useRef(null);
  useEffect(() => {
    if (!decisionPayload || typeof decisionPayload !== 'object') return;
    const payload = decisionPayload;
    // Normalize keys. The payload may use snake_case or camelCase.
    const normalized = {};
    const keyMap = {
      name: 'name', description: 'description',
      project: 'project', model: 'model',
      prompt_identity: 'prompt_identity', prompt_identity_l1: 'prompt_identity',
      prompt_boundary: 'prompt_boundary', prompt_constraint_l2: 'prompt_boundary',
      prompt_reasoning: 'prompt_reasoning', prompt_process_l3: 'prompt_reasoning',
      prompt_tools: 'prompt_tools', prompt_tools_l4: 'prompt_tools',
      prompt_output: 'prompt_output', prompt_output_l5: 'prompt_output',
      skills: 'skills', capabilities: 'capabilities',
    };
    for (const [k, v] of Object.entries(payload)) {
      const target = keyMap[k] || keyMap[k.toLowerCase()] || null;
      if (target) normalized[target] = v;
    }
    // Also accept nested agent_data / agent fields
    if (payload.agent_data && typeof payload.agent_data === 'object') {
      for (const [k, v] of Object.entries(payload.agent_data)) {
        const target = keyMap[k] || keyMap[k.toLowerCase()] || null;
        if (target) normalized[target] = v;
      }
    }
    if (payload.agent && typeof payload.agent === 'object') {
      for (const [k, v] of Object.entries(payload.agent)) {
        const target = keyMap[k] || keyMap[k.toLowerCase()] || null;
        if (target) normalized[target] = v;
      }
    }
    const prevPayload = prevPayloadRef.current || {};
    prevPayloadRef.current = payload;

    setLocalConfig((prev) => {
      const next = { ...prev };
      for (const [k, v] of Object.entries(normalized)) {
        if (v === undefined || v === null) continue;
        // Only merge if user hasn't manually touched this field
        if (dirtyFields.has(k)) continue;
        // For skills: only merge if the new value is a superset / different
        if (k === 'skills') {
          const prevSkills = JSON.stringify(prev.skills || []);
          const newSkills = JSON.stringify(Array.isArray(v) ? v : []);
          if (prevSkills === newSkills) continue;
          next.skills = Array.isArray(v) ? v : [v];
          continue;
        }
        if (k === 'capabilities') {
          const capStr = typeof v === 'string' ? v : (Array.isArray(v) ? v.join(', ') : '');
          next.capabilities = capStr;
          continue;
        }
        next[k] = v;
      }
      return next;
    });
  }, [decisionPayload]); // intentionally NOT react to dirtyFields changes

  // ── Draft: check on mount ──
  useEffect(() => {
    if (draftChecked.current) return;
    draftChecked.current = true;
    const formDraft = loadDraft(DRAFT_KEY_FORM);
    if (formDraft) {
      setName(formDraft.name || '');
      setDescription(formDraft.description || '');
      setCapabilities(formDraft.capabilities || '');
      setProject(formDraft.project || 'global');
      setDraftDialog(true);
    }
  }, []);

  // ── Load project list (cached on backend) so we can resolve project
  //    ids → names when writing the legacy `project` field for back-compat.
  useEffect(() => {
    base44.entities.Project.filter({ status: 'active' })
      .then((ps) => setProjects(ps))
      .catch(() => setProjects([]));
  }, []);

  function discardDraft() {
    clearDraft(DRAFT_KEY_FORM);
    clearDraft(DRAFT_KEY_CONFIG);
    setName(''); setDescription(''); setCapabilities(''); setProject('global'); setLlmModelId(null);
    setDraftDialog(false);
    // Notify MySpace so its draft banner refreshes immediately.
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new Event('agent_builder_draft_changed'));
    }
  }

  function resumeDraft() {
    setDraftDialog(false);
  }

  // ── Debounced draft save for form fields ──
  const timerRef = useRef(null);
  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      // Only persist when the user has typed something — don't litter
      // localStorage with empty drafts.
      if (name.trim() || description.trim() || capabilities.trim()) {
        saveDraft(DRAFT_KEY_FORM, { name, description, capabilities, project, llmModelId }, 0);
      } else {
        clearDraft(DRAFT_KEY_FORM);
      }
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new Event('agent_builder_draft_changed'));
      }
    }, 300);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [name, description, capabilities, project]);

  // ── Debounced draft save for localConfig ──
  const configTimerRef = useRef(null);
  useEffect(() => {
    if (step !== 'config') return;
    if (configTimerRef.current) clearTimeout(configTimerRef.current);
    configTimerRef.current = setTimeout(() => {
      saveDraft(DRAFT_KEY_CONFIG, localConfig, 0);
    }, 500);
    return () => { if (configTimerRef.current) clearTimeout(configTimerRef.current); };
  }, [localConfig, step]);

  // ── Build prefill from form──────────────────────────────────────
  function buildPrefill() {
    const parts = [];
    if (name.trim()) parts.push(`Agent Name: ${name.trim()}`);
    if (description.trim()) parts.push(`Description: ${description.trim()}`);
    if (capabilities.trim()) parts.push(`Capabilities: ${capabilities.trim()}`);
    // Translate the project id → human-readable name for the AI.
    if (project && project !== 'global') {
      const found = projects.find((p) => p.id === project);
      const projectName = found ? found.name : 'Unknown';
      parts.push(`Project: ${projectName}`);
    }
    if (parts.length === 0) parts.push('Build a new agent for me.');
    else parts.push('\nPlease help me build this agent.');
    return parts.join('\n');
  }

  // ── Step 1 → Step 2 ──
  async function handleBuild() {
    const prefill = buildPrefill();
    // Persist step-1 form data into initial localConfig.
    //
    // The agent (AgentApp) has TWO project-related fields:
    //   • project_id  – the new FK to projects.id (preferred; used for joins)
    //   • project     – legacy name string, kept for backward compatibility
    //     with code that still reads the .project field (e.g. MySpace.jsx
    //     label, SessionList.jsx filter, Automation page grouping)
    //
    // We write both. For `project_id`, we use the selected id; for the name
    // string, we look it up in the loaded `projects` list. If the selected
    // value is the sentinel 'global' (Ungrouped), both fields get 'global'.
    const selectedProjectId = project || 'global';
    let selectedProjectName = 'global';
    if (selectedProjectId === 'global' || selectedProjectId === '') {
      selectedProjectName = 'global';
    } else {
      const found = projects.find((p) => p.id === selectedProjectId);
      selectedProjectName = found ? found.name : 'global';
    }
    setLocalConfig((prev) => ({
      ...prev,
      name: name.trim(),
      description: description.trim(),
      // Normalize the comma-separated input into a real array BEFORE
      // merging so the JSON column on AgentApp stores an array (which
      // is what CapabilitiesSection .map() / Chat.jsx .join() expect).
      capabilities: parseCapabilities(capabilities),
      project_id: selectedProjectId === 'global' ? null : selectedProjectId,
      project: selectedProjectName,
      llm_model_id: llmModelId || null,
    }));
    // Mark form fields as user-set so future decisionPayload won't override
    setDirtyFields(new Set(['name', 'description', 'capabilities', 'project', 'llm_model_id']));
    setStep('config');
    setEnteredWithPrefill(true);
    clearDraft(DRAFT_KEY_FORM);
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new Event('agent_builder_draft_changed'));
    }
    await startNewChat(prefill);
  }

  // ── update helper (right panel) ──
  const updateConfig = useCallback((patch) => {
    setLocalConfig((prev) => ({ ...prev, ...patch }));
    // Mark patched keys as dirty
    setDirtyFields((prev) => {
      const next = new Set(prev);
      Object.keys(patch).forEach((k) => next.add(k));
      return next;
    });
  }, []);

  // ── Chat send with agentCtx ──
  const sendWithCtx = useCallback((text) => {
    handleSend(text, null, localConfig);
  }, [handleSend, localConfig]);

  // ── Key handler for chat input ──
  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendWithCtx(input);
    }
  }

  // ── Save / create agent ──
  async function handleCreateAgent() {
    setSaving(true);
    setSaveSuccess(false);
    try {
      let id = savedAgentId;
      if (id) {
        // Update existing agent
        await base44.entities.AgentApp.update(id, localConfig);
      } else {
        // Create new via API
        const created = await base44.entities.AgentApp.create(localConfig);
        id = created.id;
        setSavedAgentId(id);
      }

      // Many-to-many: mirror the new agent's primary project_id into a
      // project_agents membership row. This is what makes the agent
      // show up in the project's "Agents" list (the membership table
      // is the source of truth). The legacy AgentApp.project_id is also
      // written above for back-compat, but the project detail page
      // primarily reads the association table now.
      if (id && localConfig.project_id) {
        try {
          const existing = await base44.entities.ProjectAgent
            .filter({ project_id: localConfig.project_id, agent_id: id })
            .catch(() => []);
          if (!Array.isArray(existing) || existing.length === 0) {
            await base44.entities.ProjectAgent.create({
              project_id: localConfig.project_id,
              agent_id: id,
              role: 'primary',
            }).catch((e) => {
              // Race / unique-constraint — ignore, membership is fine
              console.warn('ProjectAgent membership create skipped:', e);
            });
          }
        } catch (e) {
          console.warn('ProjectAgent membership mirror failed (non-fatal):', e);
        }
      }

      setSaveSuccess(true);
      clearDraft(DRAFT_KEY_CONFIG);
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new Event('agent_builder_draft_changed'));
      }
    } catch (err) {
      console.error('Agent save failed:', err);
      alert('Save failed: ' + (err?.message || 'unknown error'));
    } finally {
      setSaving(false);
    }
  }

  // ── Go to run ──
  function handleGoRun() {
    if (savedAgentId) {
      navigate(`/?agent=${savedAgentId}`);
    }
  }

  // ── Back / close handlers ──
  function handleBackToForm() {
    if (messages.length > 0 && !confirm('Return to form? You will lose the current chat context.')) return;
    setStep('form');
    setEnteredWithPrefill(false);
    setMessages([]);
  }

  function handleClose() {
    if (step === 'config' && messages.length > 0) {
      // Draft is auto-saved; just close
    }
    if (onClose) onClose();
  }

  // ── Compute if we have at least some config to show ──
  const hasConfigContent = localConfig.name || localConfig.prompt_identity || localConfig.skills.length > 0 || decisionPayload;

  // ── Render ──
  return (
    <div className="flex h-full flex-col bg-background">
      {/* ---- Header bar ---- */}
      <div className="flex items-center gap-3 border-b border-border px-5 py-3 shrink-0">
        <Button variant="ghost" size="icon" onClick={onClose} disabled={loading}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div>
          <h2 className="text-sm font-semibold">{t.agentBuilder?.title || 'Create Agent'}</h2>
          <p className="text-[11px] text-muted-foreground">{t.agentBuilder?.madeBy || '由 Minha 提供'}</p>
        </div>
        <div className="flex-1" />
        {step === 'config' && (
          <Button variant="ghost" size="sm" onClick={handleBackToForm} disabled={loading}>
            <ArrowLeft className="h-3 w-3 mr-1" /> {t.agentBuilder?.backToForm || 'Back'}
          </Button>
        )}
      </div>

      {/* ---- Body ---- */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {step === 'form' ? renderStep1() : renderStep2()}
      </div>

      {/* ---- Draft resume dialog ---- */}
      {draftDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setDraftDialog(false)}>
          <div className="w-full max-w-sm rounded-xl border border-border bg-card p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-sm font-semibold mb-2">{t.agentBuilder?.draft?.title || 'Resume draft?'}</h3>
            <p className="text-xs text-muted-foreground mb-4">{t.agentBuilder?.draft?.desc || 'You have an unsaved creation in progress. Would you like to continue?'}</p>
            <div className="flex gap-2 justify-end">
              <Button variant="ghost" size="sm" onClick={discardDraft}>
                <Trash2 className="h-3 w-3 mr-1" /> {t.agentBuilder?.draft?.discard || 'Discard'}
              </Button>
              <Button size="sm" onClick={resumeDraft}>{t.agentBuilder?.draft?.resume || 'Continue'}</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );

  // ════════════════════════  STEP 1  ════════════════════════
  function renderStep1() {
    return (
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-lg px-5 py-6">
          {/* ── Step indicator card → templates → form fields → build CTA ── */}
          <AgentCreateForm
            name={name}
            onNameChange={setName}
            description={description}
            onDescriptionChange={setDescription}
            capabilities={capabilities}
            onCapabilitiesChange={setCapabilities}
            project={project}
            onProjectChange={setProject}
            llmModelId={llmModelId}
            onLlmModelIdChange={setLlmModelId}
            initialProjectId={initialProjectId}
            onBuild={handleBuild}
            building={initializing}
            currentStep={step}
          />
        </div>
      </div>
    );
  }

  // ════════════════════════  STEP 2  ════════════════════════
  function renderStep2() {
    // The hero (centered icon + input) was designed for a brand-new
    // empty chat session. Once the user has come through Step 1
    // ("Entered with prefill"), the conversation already has a
    // submitted user message — showing the hero even briefly is
    // jarring, so we drop straight into the messages-first chat
    // layout. We still show the hero when a user clicks "Back" into a
    // blank conversation (messages.length === 0 && !enteredWithPrefill).
    const showHero = messages.length === 0 && !enteredWithPrefill;

    return (
      <PanelGroup direction="horizontal" className="h-full">
        {/* ── LEFT: Chat panel (resizable) ── */}
        <Panel id="builder-chat" order={1} defaultSize={42} minSize={25} maxSize={75} className="flex flex-col min-h-0">
          <div className="flex h-full min-h-0 flex-col border-r border-border">
            {/* Messages (or centered hero when empty) */}
            {showHero ? (
              <div className="flex flex-1 flex-col items-center justify-center px-6 pb-10">
                <div className="mb-5 flex h-12 w-12 items-center justify-center rounded-full bg-secondary">
                  <Bot className="h-6 w-6 text-primary" />
                </div>
                <h1 className="font-display text-2xl tracking-tight text-foreground">
                  {t.agentBuilder?.title || 'Agent Builder'}
                </h1>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t.agentBuilder?.description || '通过对话构建智能体的专家'}
                </p>
                <div className="mt-6 w-full max-w-3xl">
                  <BuilderChatInput
                    input={input}
                    onInputChange={setInput}
                    onSend={() => sendWithCtx(input)}
                    onKeyDown={handleKeyDown}
                    disabled={loading}
                    placeholder={t.agentBuilder?.placeholder || t.agentBuilder?.chatPlaceholder || '描述你想构建的智能体…'}
                    inputRef={inputRef}
                  />
                  <div className="mt-3 flex items-center justify-center gap-1.5 text-[11px] text-muted-foreground">
                    <MessageSquare className="h-3 w-3" />
                    {t.agentBuilder?.draftHint || 'Edits are auto-saved locally'}
                  </div>
                </div>
              </div>
            ) : (
              <>
                <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
                  {initializing && (
                    <div className="flex items-center gap-3 rounded-xl border border-border bg-card px-4 py-3">
                      <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                      <span className="text-sm text-muted-foreground">{t.agentBuilder?.initializing || 'Starting conversation…'}</span>
                    </div>
                  )}
                  {messages.map((msg, idx) => (
                    <BuilderMessageBubble
                      key={msg.id || idx}
                      message={msg}
                      onOptionSelect={handleSelectOption}
                      conversationId={null}
                      hideInternalTools
                    />
                  ))}
                  {loading && streamingContent && (
                    <div className="rounded-xl border border-border bg-card px-4 py-3">
                      <p className="whitespace-pre-wrap text-sm">{streamingContent}</p>
                    </div>
                  )}
                </div>
                {/* Bottom-aligned chat input once conversation has started */}
                <div className="border-t border-border px-3 py-2.5 shrink-0">
                  <BuilderChatInput
                    input={input}
                    onInputChange={setInput}
                    onSend={() => sendWithCtx(input)}
                    onKeyDown={handleKeyDown}
                    disabled={loading || initializing}
                    placeholder={t.agentBuilder?.chatPlaceholder || 'Describe your needs…'}
                    inputRef={inputRef}
                  />
                </div>
              </>
            )}
          </div>
        </Panel>

        {/* ── Resize handle ── */}
        <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50 data-[resize-handle-state=drag]:bg-primary" />

        {/* ── RIGHT: Config preview (resizable) ── */}
        <Panel id="builder-config" order={2} defaultSize={58} minSize={25} className="flex flex-col min-h-0">
          <div className="flex h-full min-h-0 flex-col overflow-hidden">
            {/* Scrollable config area */}
            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
              {/* Save success banner */}
              {saveSuccess && (
                <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 flex items-center gap-3">
                  <Check className="h-5 w-5 text-emerald-500 shrink-0" />
                  <div className="flex-1">
                    <p className="text-sm font-medium text-emerald-400">{t.agentBuilder?.success?.title || 'Agent created!'}</p>
                    <p className="text-xs text-emerald-400/70">{t.agentBuilder?.success?.desc || 'Your agent is ready to run.'}</p>
                  </div>
                  <Button size="sm" onClick={handleGoRun} className="gap-1 shrink-0">
                    {t.agentBuilder?.success?.goRun || 'Go Run'} <ArrowRight className="h-3 w-3" />
                  </Button>
                </div>
              )}

              {/* Empty state when no config data yet */}
              {!hasConfigContent && !saveSuccess && (
                <div className="flex flex-col items-center justify-center py-20 text-center">
                  <Edit3 className="h-8 w-8 text-muted-foreground/30 mb-3" />
                  <p className="text-xs text-muted-foreground">{t.agentBuilder?.waitingConfig || 'The agent configuration will appear here as the builder generates it…'}</p>
                </div>
              )}

              {/* Config sections */}
              {hasConfigContent && (
                <>
                  <div className="rounded-lg border border-border bg-card px-4 py-3">
                    <RoleSection form={localConfig} update={updateConfig} t={t} isRoot />
                  </div>
                  <div className="rounded-lg border border-border bg-card px-4 py-3">
                    <PromptSection form={localConfig} update={updateConfig} t={t} />
                  </div>
                  <div className="rounded-lg border border-border bg-card px-4 py-3">
                    <SkillsSection form={localConfig} update={updateConfig} t={t} />
                  </div>
                </>
              )}
            </div>

            {/* Bottom action bar */}
            <div className="border-t border-border px-4 py-3 shrink-0 flex items-center gap-3">
              <div className="flex-1 text-[11px] text-muted-foreground">
                {t.agentBuilder?.draftHint || 'Edits are auto-saved locally.'}
              </div>
              {!saveSuccess && (
                <Button
                  onClick={handleCreateAgent}
                  disabled={saving || !hasConfigContent || loading}
                  className="gap-1.5"
                >
                  {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                  {t.agentBuilder?.saveAgent || 'Create Agent'}
                </Button>
              )}
              {saveSuccess && (
                <Button variant="outline" size="sm" onClick={handleGoRun} className="gap-1.5">
                  {t.agentBuilder?.success?.goRun || 'Go Run'} <ArrowRight className="h-3 w-3" />
                </Button>
              )}
            </div>
          </div>
        </Panel>
      </PanelGroup>
    );
  }
}
