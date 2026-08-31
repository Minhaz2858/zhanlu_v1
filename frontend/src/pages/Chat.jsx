import { useState, useEffect, useRef, useMemo } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { appParams } from '@/lib/app-params';
import { authFetch } from '@/api/authFetch';
import { useLanguage } from '@/lib/LanguageProvider';
import { useChatSession } from '@/lib/ChatSessionContext';
import { toast } from 'sonner';
import ChatInput from '@/components/chat/ChatInput';
import MessageBubble from '@/components/chat/MessageBubble';
import { postMessageFeedback, postRoleFeedback } from '@/api/agentFeedback';
import RelatedContent from '@/components/chat/RelatedContent';
import ArtifactPanel from '@/components/chat/ArtifactPanel';
import ArtifactPreviewPane from '@/components/chat/ArtifactPreviewPane';
import FilePreviewPane from '@/components/chat/FilePreviewPane';
import { Bot, Factory, Wrench, ShieldCheck, HardHat, Truck, Gauge, FolderOpen, FolderKanban, ArrowLeft, Clock, LayoutDashboard } from 'lucide-react';
import { PanelGroup, Panel, PanelResizeHandle } from 'react-resizable-panels';
import ChatFilesModal from '@/components/chat/ChatFilesModal';
import ScheduledPanel from '@/components/chat/ScheduledPanel';
import ArtifactCanvasEditor from '@/components/chat/ArtifactCanvasEditor';
import AutomationDraftCard from '@/components/chat/AutomationDraftCard';
import { buildSkillContext, buildDefaultSkillContext } from '@/lib/skillContext';
import { watchForDashboardEdits } from '@/lib/dashboardStreamWatcher';
import {
  streamAgentResponse,
  createAgentConversation,
  steerAgentConversation,
} from '@/api/agentEnhanced';
import { SessionExpiredError } from '@/api/authFetch';
import { usePersistentStream } from '@/lib/PersistentStreamContext';
import { dedupeMessagesByFingerprint } from './messageDedup';
import { useScreenSize } from '@/hooks/useScreenSize';
import { classifyIntent, formatHint } from '@/lib/intentClassifier';
import { detectLang } from '@/lib/detectLang';
import { shouldNotify, notificationBody } from '@/lib/completionNotify';
import { detectFileFormatRequest } from '@/lib/fileFormatRequest';
import { saveDraft, clearDraft } from '@/lib/draftManager';
import { decideRefresh, isRunInFlight } from '@/lib/chatPoll';
import { pickCreateProject } from '@/lib/createResourceProject';
import { cancelAutomationExecution } from '@/lib/cancelAutomationExecution';

const CATEGORIES = [
  { key: 'production', icon: Factory },
  { key: 'maintenance', icon: Wrench },
  { key: 'quality', icon: ShieldCheck },
  { key: 'safety', icon: HardHat },
  { key: 'supply', icon: Truck },
  { key: 'energy', icon: Gauge },
];

export default function Chat() {
  const { t, lang, aiLang } = useLanguage();
  const navigate = useNavigate();
  const isEn = lang === 'en';
  // 桌面分辨率档位（见 lib/screen-config.js）：驱动下方五个面板的
  // 默认/最小/最大尺寸，保证小屏（compact）不挤压聊天区、大屏
  // （wide/ultra）侧面板适度放大。仅影响面板尺寸，不触碰消息逻辑。
  const { settings } = useScreenSize();
  const { panels } = settings;

  // --- Session state (lifted to ChatSessionContext) -------------------------
  // The session list, active selection, and "next new chat will land in this
  // project" staging all live in the context now. The unified sidebar
  // (`Sidebar.jsx`) reads them to render the list; this page reads them to
  // drive the active conversation. This split was introduced when the chat
  // session list was merged into the main nav sidebar (Option A: unified
  // sidebar) — see `ChatSessionContext.jsx` for the full rationale.
  const {
    activeId,
    sessions,
    pendingProject,
    pendingProjectId,
    setActiveId,
    setPendingProject,
    prependSession,
    touchSession,
    adoptSessionProject,
    getSession,
    newChat: contextNewChat,
    refreshSessions,
    deleteSession,
    renameSession,
    selectSession,
    chatGeneration,
  } = useChatSession();

  const location = useLocation();
  const isAutomationSession = location.pathname === '/automation/chat';

  const [messages, setMessages] = useState([]);

  // How often to surface the "Relevant to your role?" rating (every Nth
  // assistant message). Mirrors backend ROLE_FEEDBACK_THROTTLE default.
  const ROLE_FEEDBACK_THROTTLE = 3;

  // Pre-compute the file-format request associated with each assistant
  // message, derived from the most recent user message before it. Used
  // to enforce strict format matching in the inline artifact preview
  // (the user asked for a DOCX, so only DOCX artifacts are previewed).
  const userFormatByMessage = useMemo(() => {
    const map = {};
    let last = null;
    for (const m of messages || []) {
      if (m.hidden) continue;
      if (m.role === 'user') {
        last = detectFileFormatRequest(m.content);
      } else if (m.role === 'assistant' && last) {
        map[m.id] = last;
      }
    }
    return map;
  }, [messages]);
  const [loading, setLoading] = useState(false);
  // Chat-input draft: persisted to localStorage via the shared
  // ``draftManager`` so a typed-but-not-sent message survives a page
  // refresh (and a route change that re-mounts this page). Cleared
  // on every successful send (see handleSend / handleClear /
  // handleBatchClarifySubmit / handleNewChat) so the user doesn't
  // accidentally re-send a stale draft after switching sessions.
  //
  // We use a single global key (``chat_input_draft``) rather than
  // a per-session key because the chat page is the only place the
  // input is rendered and a stale draft is harmless — the next
  // ``onChange`` overwrites it and the next send clears it.
  const [inputValue, setInputValue] = useState(() => {
    try {
      const raw = typeof window !== 'undefined'
        ? window.localStorage.getItem('draft:chat_input_draft')
        : null;
      if (!raw) return '';
      const parsed = JSON.parse(raw);
      return typeof parsed === 'string' ? parsed : '';
    } catch {
      return '';
    }
  });
  // Mirror inputValue to localStorage on every change. The chat
  // input is the only place the user is actively typing, so we
  // disable draftManager's debounce (debounceMs=0) — a "type then
  // immediately refresh" gesture would otherwise lose the last
  // keystrokes that arrived within the debounce window. The cost
  // is one localStorage.setItem per keystroke, which is negligible.
  // The effect runs after the initial mount, which is fine because
  // the useState initializer already restored the value above.
  useEffect(() => {
    saveDraft('chat_input_draft', inputValue, 0);
  }, [inputValue]);

  // Flush the latest inputValue to localStorage *synchronously* on
  // page-unload / tab-hide so a fast refresh (F5 / Ctrl-R / close-tab)
  // can't drop the keystrokes that are still sitting in React's commit
  // queue. The mirror effect above only fires AFTER React commits; if
  // the user refreshes in the same frame as their last keystroke, the
  // queued effect is discarded by the browser. The handlers below run
  // while the page is still alive and write directly, so the next load
  // sees the latest value.
  const inputValueRef = useRef(inputValue);
  useEffect(() => { inputValueRef.current = inputValue; }, [inputValue]);
  useEffect(() => {
    const flush = () => {
      try {
        localStorage.setItem(
          'draft:chat_input_draft',
          JSON.stringify(inputValueRef.current ?? ''),
        );
      } catch { /* localStorage unavailable — best effort */ }
    };
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') flush();
    };
    window.addEventListener('beforeunload', flush);
    window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('beforeunload', flush);
      window.removeEventListener('pagehide', flush);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  // Handle ?session=<chat_session_id> deep links: when /automation's Run now
  // button fires, it navigates here so the user can watch the run stream
  // live in the conversation that created the task. The session id is
  // AutomationTask.session_id, NOT the AgentConversation id used by ?conv=.
  // (For tasks without a session_id, /automation uses ?newTask=1 + ?prefill=
  // instead — see the next useEffect below.)
  //
  // Why this effect re-runs on `sessions` / `activeId` change: the
  // ChatSessionContext loads its session list asynchronously, so
  // when the user first lands on /chat?session=<sid> the list is
  // often empty — ``getSession(target)`` returns null and the old
  // effect bailed without ever calling ``handleSelectSession``.
  // Now we switch the active session unconditionally and use
  // ``getSession`` only for the (optional) URL rewrite from
  // ``?session=`` to the canonical ``?conv=<conversation_id>`` form.
  useEffect(() => {
    try {
      const target = new URLSearchParams(window.location.search).get('session');
      if (!target) return;
      // Switch the active session if it hasn't been switched yet.
      // (Re-runs when activeId / sessions change are no-ops because
      // ``target !== activeId`` is false after the first switch.)
      if (target !== activeId && handleSelectSession) {
        handleSelectSession(target);
      }
      // Update the URL to use ?conv=<conversation_id> if the session
      // is now in the loaded list. Runs every time ``sessions``
      // changes so the rewrite happens as soon as the list arrives.
      // The set+delete pair is idempotent — re-running on a URL
      // that's already in ?conv= form is a no-op.
      const exists = getSession ? getSession(target) : null;
      if (exists?.conversation_id) {
        const url = new URL(window.location.href);
        url.searchParams.set('conv', exists.conversation_id);
        url.searchParams.delete('session');
        window.history.replaceState({}, '', url.toString());
      }
    } catch { /* SSR / test env */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, sessions]);
  const [chips, setChips] = useState([]);
  const [activeSkill, setActiveSkill] = useState(null);
  const [activeAgent, setActiveAgent] = useState(null);
  const [previewResult, setPreviewResult] = useState(null);
  // File preview pane (right side) for attachment cards clicked in chat
  // history — same FilePreviewer the draft-chip modal uses, but anchored
  // in a resizable right panel (Kimi/ChatGPT-style) instead of a new tab.
  const [openFilePreview, setOpenFilePreview] = useState(null);
  const [openArtifact, setOpenArtifact] = useState(null);
  const [filesOpen, setFilesOpen] = useState(false);
  // Scheduled automation panel (Manus-style). Only shown when the current
  // session has an AutomationTask linked to it (session_id == task.session_id).
  const [scheduledOpen, setScheduledOpen] = useState(false);
  // Number of KnowledgeBases the project's data_source_runtime will
  // inject into whichever agent is selected (see
  // _extend_with_project_kbs in data_source_runtime.py). Surfaced as
  // a small badge on the project chip so users see *why* the agent
  // can talk to a DB they didn't explicitly bind to the agent.
  const [inheritedKbCount, setInheritedKbCount] = useState(0);
  // Deep-link target: when a "📅 Scheduled update" chat message's "Open run"
  // button is clicked, we open the Scheduled panel and focus this run.
  const [focusExecutionId, setFocusExecutionId] = useState(null);
  // Interactive artifact canvas (P0-3): user-editable HTML artifacts.
  const [canvasArtifact, setCanvasArtifact] = useState(null);
  const openRun = (executionId) => {
    setFocusExecutionId(executionId);
    setScheduledOpen(true);
  };
  // The automation task for the current session (null if none). Fetched
  // whenever the active session changes so we can decide whether to render
  // the "Scheduled" button in the header.
  const [sessionAutomation, setSessionAutomation] = useState(null);
  // Dashboard-mode context for the ACTIVE conversation. When the conversation
  // was stamped metadata.mode === 'dashboard' (a dedicated dashboard-building
  // session), the header shows a Dashboard badge with the bound app name and
  // an "Open dashboard" link — so the user always knows this session owns a
  // dashboard and can jump straight to it. Reopening the session restores
  // full build/edit context because the messages history is intact.
  const [dashboardMode, setDashboardMode] = useState(null); // {slug, name}
  // Latest execution of this session's automation task (from the by-session
  // poll). Drives the "agent is working" indicator while a run is in flight.
  const [latestExecution, setLatestExecution] = useState(null);
  const [attachments, setAttachments] = useState([]);
  const [streamingId, setStreamingId] = useState(null);
  const [streamingConvId, setStreamingConvId] = useState(null);  // P2: conv id of the in-flight v3 stream (for steer routing)
  // Completion notifications (Kimi/GPT-style): dedupe per run so a
  // run that completes through multiple code paths fires one toast.
  const notifiedRunIds = useRef(new Set());
  function onRunComplete(runId, finalText) {
    if (!runId || notifiedRunIds.current.has(runId)) return;
    notifiedRunIds.current.add(runId);
    if (!shouldNotify({
      hidden: typeof document !== 'undefined' && !!document.hidden,
      permission: typeof Notification !== 'undefined' ? Notification.permission : 'denied',
      hasFinalMessage: typeof finalText === 'string' && finalText.trim().length > 0,
    })) return;
    try {
      new Notification('Zhanlu', { body: notificationBody(finalText) });
    } catch { /* some browsers need a service worker — best-effort */ }
  }
  // Mirror streamingId into a ref so async helpers (loadMessages merge) can
  // read the latest value without capturing a stale closure — the polling
  // effect only depends on activeId, so it would otherwise freeze streamingId.
  const streamingIdRef = useRef(null);
  useEffect(() => { streamingIdRef.current = streamingId; }, [streamingId]);
  // Track which session loadMessages is currently fetching for, so the
  // merge inside the functional updater can reject stale messages from a
  // previous session. Set synchronously before every loadMessages call.
  const loadSessionRef = useRef(null);
  // Experience layer (Phase C): per-message feedback state for the
  // thumbs up/down buttons on assistant bubbles. Optimistic — set
  // immediately, reverted if the API call fails.
  const [feedbackByMessage, setFeedbackByMessage] = useState({});
  const handleMessageFeedback = async (messageId, rating) => {
    const s = activeId ? getSession(activeId) : null;
    const convId = s?.conversation_id;
    if (!convId || !messageId) {
      toast.error('无法提交反馈：会话信息不完整');
      return;
    }
    const prev = feedbackByMessage[messageId];
    // Toggle off when the same rating is clicked again.
    if (prev === rating) {
      setFeedbackByMessage((p) => ({ ...p, [messageId]: null }));
      return;
    }
    setFeedbackByMessage((p) => ({ ...p, [messageId]: rating }));
    try {
      await postMessageFeedback(appParams.appId, convId, messageId, rating);
      toast.success(rating > 0 ? '已记录好评，感谢反馈！' : '已记录差评，我们会持续改进。');
    } catch (err) {
      setFeedbackByMessage((p) => ({ ...p, [messageId]: prev }));
      toast.error(`反馈提交失败：${err.message || err}`);
    }
  };
  // Role-based personalization: per-message 1-5 "Relevant to your role?"
  // rating state, collected only on throttled assistant messages (every
  // ROLE_FEEDBACK_THROTTLE-th assistant message) to avoid fatigue.
  const [roleRelevanceByMessage, setRoleRelevanceByMessage] = useState({});
  // Which assistant message ids should surface the role-relevance rating.
  // Throttled to every ROLE_FEEDBACK_THROTTLE-th assistant message (by
  // chronological order in the current conversation) to reduce fatigue.
  const roleRatingMessageIds = useMemo(() => {
    const ids = new Set();
    let assistantCount = 0;
    for (const m of messages) {
      if (m.role === 'assistant' && (m.content || '').trim()) {
        assistantCount += 1;
        if (assistantCount % ROLE_FEEDBACK_THROTTLE === 0) ids.add(m.id);
      }
    }
    return ids;
  }, [messages]);
  const handleRoleRelevance = async (messageId, rating) => {
    const s = activeId ? getSession(activeId) : null;
    const convId = s?.conversation_id;
    if (!convId || !messageId) {
      toast.error('无法提交角色相关度反馈：会话信息不完整');
      return;
    }
    const prev = roleRelevanceByMessage[messageId];
    if (prev === rating) {
      setRoleRelevanceByMessage((p) => ({ ...p, [messageId]: null }));
      return;
    }
    setRoleRelevanceByMessage((p) => ({ ...p, [messageId]: rating }));
    try {
      await postRoleFeedback(appParams.appId, convId, messageId, rating);
      toast.success('已记录角色相关度评分，感谢反馈！');
    } catch (err) {
      setRoleRelevanceByMessage((p) => ({ ...p, [messageId]: prev }));
      toast.error(`角色相关度反馈提交失败：${err.message || err}`);
    }
  };
  // P2 (conversational dashboards): persistent dock panel state. `open` toggles
  // mount/unmount of the panel; `viewerRef` lets the stream watcher tell the
  // DashboardViewer to refetch without prop-drilling. `seenRef` dedupes the
  // watcher per message id so the panel doesn't re-open on every chunk.
  const dashboardSeenRef = useRef(new Map());
  const [loadStatus, setLoadStatus] = useState('idle'); // 'idle' | 'loading' | 'loaded' | 'error'
  // P3: pending automation draft — shown as an editable card right after
  // the user submits the New-Automation dialog. The prefill is captured
  // here, parsed into fields, and the user can refine any field inline
  // before the agent actually creates the task. The card auto-dismisses
  // when the agent confirms creation, or when the user clicks ×.
  const [pendingDraft, setPendingDraft] = useState(null); // { prefill: string, lang: 'zh' | 'en' } | null
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  // genRef and stream lifecycle now come from PersistentStreamContext, which
  // survives page navigation so streaming continues in the background.
  const stream = usePersistentStream();
  const genRef = stream.genRef; // alias for shorter line references below
  const persistTimerRef = useRef(null);
  // P3-bis: guard for the ?autorun=1 deep link (used by /automation's
  // "Run Now" button). The autorun useEffect below depends on
  // ``activeId`` and re-runs whenever the session changes; without this
  // ref a same-session re-render would re-send the prefill and
  // duplicate the user message. Reset on URL change (the effect
  // itself checks the URL on every run).
  const autorunProcessedRef = useRef(false);

  // Poll re-attach nonce: bumped by the ``automation:run-started``
  // listener (defined alongside the by-session poller) whenever an
  // external Run Now signal lands. The poller normally stops once
  // ``decideRefresh`` sees a stable-terminal execution — a subsequent
  // Run Now on the same task would otherwise stay invisible until the
  // user manually refreshes, because the dep array (`[activeId]`)
  // doesn't change for a same-session event. The ref mirror exists so
  // imperative code (or future debugging tools) can read the latest
  // value without subscribing to a re-render.
  const tickPollReattachRef = useRef(0);
  const [pollAttachNonce, setPollAttachNonce] = useState(0);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const prefill = params.get('prefill');
    // `?newTask=1` flags that the user came from the New-Automation
    // dialog (or similar structured creation entry point) and wants a
    // brand-new session — never append to the currently-active one.
    // Combined with the project params, this routes the prefill into a
    // fresh chat that lives in the right project, mirroring the
    // Manus-style "one chat per task" flow.
    const newTask = params.get('newTask') === '1';
    const dialogProjectName = params.get('projectName') || null;
    const dialogProjectId = params.get('projectId') || null;
    // `?autorun=1` is set by /automation's "Run Now" button — it
    // hands the run off to the LLM by injecting a structured
    // "Run Automation Task: ..." prompt and auto-sending it. The
    // auto-send has to wait for ``activeId`` to be set by the
    // session deep-link effect above, so it's handled in a separate
    // useEffect below that depends on ``activeId``. Skip the
    // prefill processing here to avoid double-sending.
    const autorun = params.get('autorun') === '1';
    if (prefill && !autorun) {
      // `?newTask=1` is set by the structured-creation dialogs (New Task /
      // Save & Continue in Chat). It means: "open a BRAND-NEW chat session
      // for this prefill, bound to the selected project" — for EVERY
      // resource type (automation, dashboard, report, kb, file, …), not just
      // automation requests. The prefill is always auto-sent into that fresh
      // session so the user lands in a clean "one task per chat" context
      // (Manus-style), mirroring the dialog's promise of "continue in chat".
      if (isAutomationRequest(prefill)) {
        // Stash the prefill as a pending draft BEFORE sending so the
        // editable summary card can mount before the agent starts
        // streaming. The card stays visible until the user dismisses
        // it (or the agent confirms the task is created).
        setPendingDraft({ prefill, lang });
      }
      if (newTask) {
        // Resolve the new-task options. We prefer the dialog's project
        // (if provided) so the user gets the project they were just in,
        // not whatever project the sidebar happens to have selected.
        handleSend(prefill, {
          newSessionTitle: t.sessionList.newTask,
          newSessionProject: dialogProjectName,
          newSessionProjectId: dialogProjectId,
        });
        // Strip the newTask/project params from the URL so a refresh
        // (or back-button) doesn't re-trigger the same flow.
        try {
          const newParams = new URLSearchParams(window.location.search);
          newParams.delete('newTask');
          newParams.delete('projectName');
          newParams.delete('projectId');
          const qs = newParams.toString();
          const newUrl = window.location.pathname + (qs ? `?${qs}` : '') + window.location.hash;
          window.history.replaceState({}, '', newUrl);
        } catch { /* best-effort — the state is already correct */ }
      } else if (isAutomationRequest(prefill)) {
        // Legacy path (no newTask flag): automation prefills auto-send into
        // the currently-active session; anything else is staged in the input
        // for the user to review before sending.
        handleSend(prefill, {});
      } else {
        setInputValue(prefill);
      }
    }
    // Project-scoped entry point: when the user clicks an agent on a
    // project's page we land here with ?project=<id>&projectName=<name>.
    // Stage the project into the context so the next ChatSession
    // created from this page is auto-tagged with the right project.
    const projectId = params.get('project');
    const projectName = params.get('projectName');
    if (projectName) {
      try { setPendingProject(decodeURIComponent(projectName), projectId || null); } catch { setPendingProject(projectName, projectId || null); }
    } else if (projectId) {
      setPendingProject(null, projectId);
    }
    // Stash the project context so the agent conversation can be
    // tagged with the same project_id (so data-source runtime
    // inherits project KBs).  This must happen even when no agent
    // is specified — the project-only chat path auto-selects
    // ``general_assistant`` and needs this context to scope the
    // data-source runtime.
    if (projectId) {
      try {
        sessionStorage.setItem('zhanlu:lastProjectContext', JSON.stringify({
          projectId,
          projectName: projectName ? decodeURIComponent(projectName) : '',
        }));
      } catch { /* noop */ }
    }
    const agentId = params.get('agent');
    if (agentId) {
      base44.entities.AgentApp.get(agentId).then((agent) => {
        setActiveAgent(agent);
        // The chat input no longer shows a "DB: …" chip or a "Read
        // from my database" quick action — the agent's data-source
        // binding is still consumed by the data-source runtime at
        // execution time. We deliberately don't resolve the bound
        // Kbs here to keep this code path minimal.
      }).catch(() => { });
    }
    // Open an existing agent conversation: the user clicked a row
    // in the Project Detail "Recent Chats" list. We rehydrate the
    // chat pane with the conversation's stored messages and set
    // the active agent from the conv's ``agent_name`` (looked up
    // by name — we don't get the agent id from the conv record).
    //
    // Note: agent_name is stored on the conv record, but we accept
    // ?agentName= as a query-param override for two reasons:
    //   1. It avoids a round-trip when the caller already has the
    //      agent name (e.g. the Recent Chats row).
    //   2. It future-proofs the case where an agent gets renamed —
    //      the conv record's agent_name is then stale, but the
    //      caller can still pass the current name.
    const convId = params.get('conv');
    const convAgentName = params.get('agentName') || null;
    if (convId) {
      // Mark this load so the activeId effect can detect the race.
      // The activeId effect overwrites loadSessionRef with the chat
      // session id — if that happens before this .then() fires, we
      // bail to avoid overwriting correctly-loaded chat_messages
      // with raw conv messages that lack session_id.
      loadSessionRef.current = `conv:${convId}`;
      base44.entities.AgentConversation.get(convId).then(async (conv) => {
        if (!conv) return;
        // Hydrate the message list from the conversation record.
        // The stored shape is ``[{role, content, tool_calls,
        // created_date, id, ...}]`` which is the same shape the
        // chat page already consumes.
        //
        // DEDUP (2026-08-06 fix): the JSON column here is one of two
        // parallel stores (the other being the chat_messages table,
        // loaded later by loadMessages). The two stores share no id
        // space, so a single logical message can appear under two
        // different uuids with different created_date values. Apply
        // the content-fingerprint dedup here so this direct set call
        // does not leak cross-store duplicates into state.
        //
        // SESSION GUARD (2026-08-12 fix): use a functional updater
        // and bail if the active session has already been set to a
        // different chat session — prevents conv messages from
        // overwriting correctly-loaded chat_messages.
        setMessages((prev) => {
          if (loadSessionRef.current !== `conv:${convId}`) return prev;
          return dedupeMessagesByFingerprint(
            Array.isArray(conv.messages) ? conv.messages : [],
          );
        });
        setLoadStatus('loaded');
        // Resolve the agent by name (the conv record stores
        // ``agent_name`` but not the AgentApp id). We do a
        // best-effort list and filter — the typical case has a
        // small number of agents so the cost is negligible. If
        // the agent can't be found, we just leave activeAgent
        // null and the runtime will fall through to the
        // no-agent path (which auto-resolves general_assistant
        // for the next user message).
        const wantedName = convAgentName || conv.agent_name;
        if (wantedName) {
          try {
            const agents = await base44.entities.AgentApp.list();
            const agent = agents.find((a) => a.name === wantedName);
            if (agent) setActiveAgent(agent);
          } catch { /* best-effort */ }
        }
        // Forward the conv's PROJECT context into the URL —
        // handleAgentSend reads project_id / project_name from
        // window.location to include them in the v3 stream body,
        // and the backend's prepare_data_source_runtime uses them
        // to scope the data-source runtime + memory snapshot to
        // the right project. Without this, legacy convs (created
        // before the body-override path was added) and reloads
        // (which never go through ``selectSession``) end up with
        // the URL as ``?conv=...`` only — the agent then runs
        // with no project context and falls back to per-user
        // memory across all projects. Also seed ``pendingProject``
        // (the chip in the chat input) and ``pendingProjectId``
        // (the FK) from the conv so the input shows the correct
        // project chip after a hard reload.
        //
        // IMPORTANT (2026-08-31): the AgentConversation model has
        // NO ``project_name`` / ``project`` columns — the name
        // lives inside ``conv.metadata.project_name`` /
        // ``conv.metadata.project`` (createAgentConversation in
        // agentEnhanced.js stores it there).  The old code read
        // ``conv.project_name || conv.project`` which is ALWAYS
        // undefined for every conversation, so ``?projectName=``
        // was never restored on refresh — the URL stayed
        // ``?conv=...&agentName=...`` and the project context
        // silently vanished.  Read from metadata instead.
        //
        // ALSO (2026-08-31, user-requested): write ``agentName``
        // back into the URL here.  The old comment below claimed
        // "no code path reads agentName from the URL" — that's
        // outdated: the ?conv= deep-link restore above DOES read
        // ``?agentName=`` as an override (convAgentName), and the
        // user explicitly wants the URL to carry the full context
        // (project + agent + conv) so a refresh keeps the LLM
        // bound to the same agent.  Matching other AI agents'
        // shareable-conversation URLs (Kimi/Claude/GPT).
        try {
          const url = new URL(window.location.href);
          if (conv.project_id) {
            url.searchParams.set('project', conv.project_id);
          }
          const pname = (conv.metadata && (conv.metadata.project_name || conv.metadata.project))
            || conv.project_name || conv.project;
          if (pname) {
            url.searchParams.set('projectName', pname);
            // Seed the input's project chip + FK together (the only
            // setter the context exposes — setPendingProject(name, id)).
            setPendingProject(pname, conv.project_id || null);
          } else if (conv.project_id) {
            // No name resolved — still stage the FK so the chip logic
            // and next-send project scoping work.
            setPendingProject(null, conv.project_id);
          }
          const agentName = wantedName || conv.agent_name;
          if (agentName) {
            url.searchParams.set('agentName', agentName);
          }
          window.history.replaceState({}, '', url.toString());
        } catch { /* SSR / non-browser env — best-effort */ }
        // Dashboard-mode: if this conversation was stamped as a dedicated
        // dashboard session (metadata.mode === 'dashboard'), surface the
        // badge in the header with a link to the bound app. The slug/name
        // were written by create_fullstack_dashboard / update_fullstack_dashboard.
        const meta = conv.metadata || {};
        if (meta.mode === 'dashboard' && (meta.dashboard_slug || meta.dashboard_id)) {
          setDashboardMode({
            slug: meta.dashboard_slug || meta.dashboard_id,
            name: meta.dashboard_name || meta.dashboard_slug || meta.dashboard_id,
          });
        } else {
          setDashboardMode(null);
        }
      }).catch(() => {
        // 404 or network error — leave the chat empty and let
        // the user start a new session.
      });
    }
    // Clean the TRULY transient params (?agent=, ?prefill=,
    // ?autorun=) from the URL but PRESERVE ?conv= — that's the
    // deep-link to the current conversation and the user must be
    // able to reload / share the page without losing the session.
    // We also PRESERVE ?project= / ?projectName= / ?agentName= —
    // these are the conv's project context and must stay in the
    // URL so handleAgentSend can forward them to the v3 stream
    // body (which scopes the data-source runtime + memory
    // snapshot to the right project). The previous version of
    // this cleanup deleted them — that's why every reload of a
    // ``?conv=...`` URL ended up with no project context, and
    // why the Q2 2026 report (and other per-user memory items)
    // leaked into every project.
    //
    // For ?autorun=1 the prefill is consumed by the autorun useEffect
    // below, so we leave ?prefill= (and ?autorun=) alone here to
    // avoid a race where this effect strips the param before the
    // autorun useEffect can read it.
    try {
      const url = new URL(window.location.href);
      const skipForAutorun = url.searchParams.get('autorun') === '1';
      // Only the truly transient one-shot params get cleared here.
      // project / projectName / agentName are sticky context
      // params; their lifetime is managed by selectSession (sidebar
      // clicks) and the conv rehydration above (reloads).
      ['agent', 'prefill', 'autorun'].forEach((k) => {
        if (skipForAutorun && (k === 'prefill' || k === 'autorun')) return;
        url.searchParams.delete(k);
      });
      window.history.replaceState({}, '', url.toString());
    } catch {
      // Older browsers / non-browser envs — fall back to the
      // previous behavior of clearing the URL entirely.
      window.history.replaceState({}, '', '/');
    }
  }, []);
  // P3-bis: ?autorun=1 deep-link from /automation's "Run Now" button.
  // The button builds a structured "Run Automation Task: ..." prompt
  // and navigates here. We:
  //   1. Wait for the session deep-link effect above to set
  //      ``activeId`` (this effect depends on it).
  //   2. Show the prompt in the input box briefly (UX: the user
  //      sees their own request as it's being typed in).
  //   3. Call ``handleSend(prefill)`` so the LLM receives the
  //      prompt and calls ``execute_automation`` — the
  //      dispatcher's ``_notify_chat`` then streams the result
  //      back into the SAME session, giving the user the
  //      "request + inline response" view they wanted.
  //   4. Strip ?prefill= and ?autorun= from the URL.
  //
  // Why a separate useEffect (not the existing prefill one):
  // the existing prefill useEffect runs once on mount with the
  // closure's ``activeId`` (which is null or stale at that point
  // because the session deep-link effect is queued in the same
  // render but its setState is committed later). Depending on
  // ``activeId`` here makes us re-run after the session is
  // actually set, so ``handleSend`` lands in the right session.
  useEffect(() => {
    if (!activeId) return;
    if (autorunProcessedRef.current) return;
    const params = new URLSearchParams(window.location.search);
    const prefill = params.get('prefill');
    const autorun = params.get('autorun') === '1';
    if (!autorun || !prefill) return;
    autorunProcessedRef.current = true;
    // Surface the prompt in the input box first so the user sees
    // the request appear as if they had typed it themselves.
    setInputValue(prefill);
    // Send it. ``handleSend`` reads ``activeId`` from its closure,
    // which is the value committed by this re-render (i.e. the
    // task's dedicated session), so the message + LLM stream
    // land in the right conversation.
    handleSend(prefill);
    // Strip the autorun params from the URL — same rationale as
    // the prefill useEffect above.
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('prefill');
      url.searchParams.delete('autorun');
      window.history.replaceState({}, '', url.toString());
    } catch { /* best-effort — the state is already correct */ }
  }, [activeId]);
  useEffect(() => {
    // Whenever the active session changes, reset transient chat-page
    // state — the preview pane, the previous result panel, any
    // attached skills/chips, and draft attachments.
    //
    // Agent clearing rule: clear ``activeAgent`` ONLY when switching to
    // an existing session (one that already has a ``conversation_id``).
    // When ``handleSend`` creates a brand-new session and calls
    // ``setActiveId(sid)``, the session exists but ``conversation_id``
    // is still null — we must NOT clear the agent the user just
    // selected, otherwise the chip disappears between "send" and
    // "conversation created". For existing sessions, clearing is
    // safe because the separate effect below re-resolves the agent
    // from the conversation's ``agent_name`` (or leaves it null for
    // agent-less general chats).
    const _s = activeId ? getSession(activeId) : null;
    const _isNewSession = activeId && (!_s || !_s.conversation_id);
    setOpenArtifact(null);
    setPreviewResult(null);
    setOpenFilePreview(null);
    setChips([]);
    setActiveSkill(null);
    if (!_isNewSession) setActiveAgent(null);
    // Only wipe draft attachments when switching to an EXISTING session
    // (one with a conversation_id). A brand-new session can be created
    // mid-upload by ensureSession() from the landing page (activeId was
    // null) — wiping here would erase the chip the user just uploaded
    // and the agent would never receive the file. Attachments for a
    // genuinely fresh task were already cleared when activeId went to
    // null (the "+ New Task" transition), so skipping the wipe for new
    // sessions is a no-op in that flow and only protects the upload case.
    if (!_isNewSession) setAttachments([]);
    if (activeId) {
      loadSessionRef.current = activeId;
      setMessages([]);
      loadMessages(activeId);
    } else {
      loadSessionRef.current = null;
      setMessages([]);
    }
    // Watch ``chatGeneration`` (NOT just ``activeId``) so this effect
    // also re-fires when the user clicks "+ New Task" in a state
    // where ``activeId`` was already null — e.g. when the chat was
    // opened via a ``?conv=<id>`` deep link, which populates
    // ``messages`` without ever setting ``activeId``. Without
    // ``chatGeneration`` in the deps, that state's "+ New Task"
    // click would silently no-op.
  }, [activeId, chatGeneration]);

  // When activeId transitions from non-null to null (user clicked
  // "+ New Task" via contextNewChat, or the active session was
  // deleted), the URL's ?conv= is now stale — a reload would resume
  // the previous conversation. SelectSession keeps the URL in sync
  // for the non-null case; this effect handles the null case.
  // handleNewChat also clears the URL explicitly (idempotent — we
  // end up here anyway because it calls contextNewChat which nulls
  // activeId).
  //
  // CRITICAL: do NOT run on initial mount. On a deep-link
  // ``?conv=<id>`` the conv-rehydration useEffect at the top of
  // the file loads the conv's messages and the deep-link restore
  // effect below looks up the matching ChatSession asynchronously
  // (sessions is loaded async). If this effect fires on mount
  // (because activeId starts as null) it strips ?conv= before the
  // restore effect ever gets a chance to find the session —
  // activeId then stays null forever and the URL is broken.
  // Track previous activeId and only clean up on the actual
  // non-null → null transition.
  const prevActiveIdRef = useRef(null);
  useEffect(() => {
    const prev = prevActiveIdRef.current;
    prevActiveIdRef.current = activeId;
    if (activeId) return;
    if (!prev) return; // Initial mount or no transition — leave the URL alone.
    try {
      const url = new URL(window.location.href);
      if (url.searchParams.has('conv')) {
        url.searchParams.delete('conv');
        window.history.replaceState({}, '', url.toString());
      }
    } catch { /* SSR / test env */ }
  }, [activeId]);

  // Deep-link restore: when the URL has ``?conv=<conversation_id>``
  // (typical after a page refresh on a chat deep link, or a shared
  // link from the sidebar's "Recent Chats"), link the conv back to
  // its owning ChatSession so the chat is fully functional (activeId
  // set, input enabled, project chip adopted). The rehydration
  // useEffect at the top of the file loads the conv's messages and
  // agent, but the conv doesn't carry the session id — only the
  // ChatSession does, and ``sessions`` is loaded asynchronously. So
  // we watch ``sessions`` and re-evaluate as soon as the list
  // arrives. Without this, ``activeId`` stays null after a refresh,
  // and the cleanup useEffect above wipes ``?conv=`` from the URL
  // (and the user can no longer type into the chat input because
  // there is no active session).
  useEffect(() => {
    if (!sessions || sessions.length === 0) return;
    try {
      const convId = new URLSearchParams(window.location.search).get('conv');
      if (!convId) return;
      const s = sessions.find((x) => x.conversation_id === convId);
      if (!s) return;
      if (activeId === s.id) return;
      if (selectSession) selectSession(s.id);
    } catch { /* SSR / test env */ }
  }, [sessions, activeId, selectSession]);

  // Auto-restore the active agent when opening a session that has a
  // linked AgentConversation (conversation_id set on the ChatSession
  // record). This is the missing piece that makes the sidebar reopen
  // flow feel project/agent-aware: previously the chat input showed no
  // project chip and no agent tag, and the next send fell through to
  // the generic LLM proxy — the agent "forgot" everything about the
  // session. We resolve the agent by name from the AgentConversation,
  // same approach as the ?conv=<id> URL handler.
  useEffect(() => {
    if (!activeId) return;
    const s = getSession(activeId);
    if (!s || !s.conversation_id) return;
    let cancelled = false;
    (async () => {
      try {
        const conv = await base44.entities.AgentConversation.get(s.conversation_id);
        if (cancelled || !conv) return;
        const wantedName = conv.agent_name;
        if (!wantedName) return;
        const agents = await base44.entities.AgentApp.list();
        const agent = agents.find((a) => a.name === wantedName);
        if (cancelled) return;
        if (agent) setActiveAgent(agent);
      } catch { /* best-effort — leave activeAgent null, the next
                   send will auto-resolve general_assistant if the
                   project chip is set */ }
    })();
    return () => { cancelled = true; };
    // Re-run when activeId changes OR when the session's
    // conversation_id field changes (e.g. a new link was just written).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, getSession ? getSession(activeId)?.conversation_id : null]);
  // Auto-scroll and the floating "Jump to latest" button were removed
  // (2026-07-29, user request): the viewport now stays wherever the
  // user put it; new messages simply append without moving the view.

  // Resolve the project's bound KnowledgeBases and surface the count
  // to the chat input as an "inheriting N data sources" badge. Mirrors
  // the server-side ``_extend_with_project_kbs`` in
  // ``data_source_runtime.py`` so the UI shows the same scope the
  // runtime will apply on the next send. Best-effort: a network failure
  // just leaves the badge hidden (count = 0), it does not block the
  // chat.
  // Mirror the server-side ``_extend_with_project_kbs``: it gates on
  // either ``selected_project_id`` OR ``selected_project_name``
  // (see data_source_runtime.py:435). When only the name is set
  // (sidebar "+ New Chat", ``?projectName=…`` without ``?project=``,
  // or a legacy row whose ``project_id`` was never populated), the
  // KB count useEffect previously short-circuited to 0 and the badge
  // silently disappeared. Fall back to filtering by the legacy
  // ``kb.project`` name column.
  useEffect(() => {
    if (!pendingProjectId && !pendingProject) {
      setInheritedKbCount(0);
      return undefined;
    }
    let cancelled = false;
    (async () => {
      try {
        const kbs = await base44.entities.KnowledgeBase.list();
        if (cancelled) return;
        const count = pendingProjectId
          ? kbs.filter((kb) => kb.project_id === pendingProjectId).length
          : kbs.filter((kb) => kb.project === pendingProject).length;
        if (!cancelled) setInheritedKbCount(count);
      } catch {
        if (!cancelled) setInheritedKbCount(0);
      }
    })();
    return () => { cancelled = true; };
  }, [pendingProjectId, pendingProject]);

  // Debounced persistence to localStorage so the chat survives refreshes
  useEffect(() => {
    if (!activeId || loadStatus !== 'loaded') return;
    if (persistTimerRef.current) clearTimeout(persistTimerRef.current);
    persistTimerRef.current = setTimeout(() => {
      try {
        const cacheKey = `zhanlu:messages:${activeId}`;
        localStorage.setItem(cacheKey, JSON.stringify(messages.slice(0, 200)));
      } catch { /* best-effort */ }
    }, 300);
    return () => {
      if (persistTimerRef.current) clearTimeout(persistTimerRef.current);
    };
  }, [messages, activeId, loadStatus]);

  // ---- Detect whether the current session has a scheduled automation -----
  // The Manus-style "Scheduled" button only appears when this session is the
  // origin of an AutomationTask (task.session_id == activeId). We fetch on
  // every activeId change so the button shows / hides as the user switches
  // between chats.
  useEffect(() => {
    if (!activeId) {
      setSessionAutomation(null);
      return undefined;
    }
    let cancelled = false;
    let timer = null;
    let pollCount = 0;
    // Previous /by-session payload so decideRefresh can detect execution
    // id/status transitions (and stop polling once the latest execution is
    // stable-terminal). Null on the first poll.
    let prevPayload = null;
    const POLL_INTERVAL_MS = 2500;
    // Safety cap so a stuck non-terminal execution (e.g. a task whose status
    // never flips back to idle) can't poll forever. Must exceed the slowest
    // realistic run: an LLM data_sync run measured ~104s (07:36:39→07:38:23),
    // and a 40-poll/100s cap gave up ~4s BEFORE the response was written —
    // the exact "agent not responding" regression. 360 polls ≈ 15 min, which
    // covers long LLM runs while still bounding a genuinely stuck execution.
    // (Quick runs stop far earlier via decideRefresh's stable-terminal stop.)
    const MAX_POLLS = 360;

    async function checkSession() {
      try {
        const res = await authFetch(`/api/automations/by-session/${activeId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled) return;
        setSessionAutomation(data.task || null);
        // Track the latest execution so the UI can show an "agent is working"
        // indicator while a run is in flight (the executor is async, so
        // without this the user sees the request bubble then silence).
        setLatestExecution(
          Array.isArray(data.executions) && data.executions.length > 0
            ? data.executions[0]
            : null,
        );
        // decideRefresh tells us whether to reload the message list (the
        // background executor writes the marker + agent response AFTER the
        // initial mount-time load, so without these reloads the user would
        // only ever see the user bubble). Silent so we don't flash the
        // empty-state spinner on a poll that has messages already.
        const decision = decideRefresh(prevPayload, data);
        if (decision.reload) {
          loadSessionRef.current = activeId;
          loadMessages(activeId, { silent: true });
        }
        prevPayload = data;
        pollCount += 1;
        if (decision.keepPolling && pollCount < MAX_POLLS) {
          timer = setTimeout(checkSession, POLL_INTERVAL_MS);
        }
      } catch {
        if (cancelled) return;
        setSessionAutomation(null);
        // Network blip — retry once more, then stop (don't spin on a dead
        // server).
        if (pollCount < 2) {
          pollCount += 1;
          timer = setTimeout(checkSession, POLL_INTERVAL_MS);
        }
      }
    }
    checkSession();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // The poll effect depends on ``activeId`` for the basic mount/swap
    // case AND on ``pollAttachNonce`` so the
    // ``automation:run-started`` listener can re-arm the poller when a
    // same-session Run Now lands after a previous stable-terminal
    // stop. Without the nonce dep, the listener's
    // ``setPollAttachNonce`` bump wouldn't cause this effect to
    // re-fire, leaving new runs invisible on a warm session.
  }, [activeId, pollAttachNonce]);

  // External run-started signal: ``runAutomationTask`` dispatches
  // ``automation:run-started`` after POST /api/automations/{id}/run
  // returns, BEFORE its navigate() call. This effect picks it up,
  // matches the session, and forces two things the existing reactive
  // plumbing can't reliably produce across a warm-then-cold mount:
  //   1. ``loadMessages`` immediately, so the user bubble + empty
  //      assistant bubble written by ``_post_run_request_marker``
  //      surface without waiting ~2.5s for the next poll cycle.
  //   2. Re-arms the by-session poller. The poll effect below stops
  //      once ``decideRefresh`` sees a stable-terminal execution — a
  //      subsequent Run Now on the same task would otherwise stay
  //      invisible until the user manually refreshes. We bump a
  //      dedicated ``pollAttachNonce`` to break the dep equality and
  //      re-fire the poll effect on demand.
  // The listener soft-fails silently so a missed signal (mobile, no
  // window, pre-fix build) doesn't break Run Now.
  useEffect(() => {
    if (!activeId) return undefined;
    const onRunStarted = (ev) => {
      const detail = ev && ev.detail;
      if (!detail || detail.sessionId !== activeId) return;
      // 1) Pull the freshly-written marker bubbles into state
      //    immediately (silent = no loading flash).
      loadSessionRef.current = activeId;
      loadMessages(activeId, { silent: true });
      // 2a) Mirror the latest attach count for any imperative readers
      //     that want the value without subscribing to a re-render.
      tickPollReattachRef.current = (tickPollReattachRef.current || 0) + 1;
      // 2b) Force the polling useEffect to re-fire. React uses shallow
      //     dep equality so bumping the ref alone won't re-evaluate
      //     the effect; the ``pollAttachNonce`` state IS the actual
      //     trigger.
      setPollAttachNonce((n) => n + 1);
    };
    window.addEventListener('automation:run-started', onRunStarted);
    return () => window.removeEventListener('automation:run-started', onRunStarted);
    // ``loadMessages`` is captured via closure — re-subscribing on
    // every setMessages churn is unnecessary because loadMessages
    // reads its own latest state through setMessages internally.
    // activeId-only is sufficient.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  async function loadMessages(sid, opts) {
    const silent = !!(opts && opts.silent);
    if (!silent) setLoadStatus('loading');
    // Cache key bumped on demand — bump "" → "v2" to invalidate any
    // pre-fix caches that hold stale "Sorry" rows now deleted from the
    // backend (the cache + DB were consistent at write time but the
    // DB-side cleanup leaves the cache out-of-date).
    const cacheKey = `zhanlu:messages:v2:${sid}`;
    let msgs = null;

    // Primary: load from backend
    try {
      // Sort by created_date (server clock), NOT `order`: the order column
      // mixes tiny sequential indexes (chat turns, ``current.length``) with
      // epoch timestamps (scheduled-run updates from ``_notify_chat``), so
      // sorting by it grouped user inputs and agent activity into separate
      // regions instead of interleaving them chronologically.
      msgs = await base44.entities.ChatMessage.filter({ session_id: sid }, 'created_date', 200);
      // Cache to localStorage on success
      try {
        localStorage.setItem(cacheKey, JSON.stringify(msgs.slice(0, 200)));
        // Also drop any v1 cache so it doesn't get used after a /v2 cache
        // miss (e.g. user private-browses or storage quota eviction).
        try { localStorage.removeItem(`zhanlu:messages:${sid}`); } catch { /* noop */ }
      } catch { /* best-effort cache write */ }
    } catch {
      // Retry once after 500ms backoff
      await new Promise((r) => setTimeout(r, 500));
      try {
        // Sort by created_date (server clock), NOT `order`: the order column
        // mixes tiny sequential indexes (chat turns, ``current.length``) with
        // epoch timestamps (scheduled-run updates from ``_notify_chat``), so
        // sorting by it grouped user inputs and agent activity into separate
        // regions instead of interleaving them chronologically.
        msgs = await base44.entities.ChatMessage.filter({ session_id: sid }, 'created_date', 200);
        try {
          localStorage.setItem(cacheKey, JSON.stringify(msgs.slice(0, 200)));
        } catch { /* best-effort */ }
      } catch {
        // Fallback to localStorage cache (v2 only — see cacheKey above)
        try {
          const cached = localStorage.getItem(cacheKey);
          if (cached) {
            msgs = JSON.parse(cached);
            toast.info('Loaded from local cache', { duration: 3000 });
          }
        } catch { /* noop */ }
      }
    }

    // Merge the fetched list with the CURRENT local state instead of blindly
    // overwriting. This polling-driven load can resolve BEFORE the just-sent
    // user message has committed to the DB (handleSend's ChatMessage.create is
    // still in flight), so a naive setMessages(msgs) wipes the just-added
    // bubble — the user sees "message sent, bubble missing" until a full page
    // refresh (by which time the server has it). We preserve:
    //   (a) local messages whose id is NOT in the fetch — in-flight bubbles the
    //       server hasn't committed yet (the user message, the just-created
    //       assistant placeholder), and
    //   (b) the actively-streaming message — its local partial content is
    //       fresher than the server's empty placeholder, so a poll mid-stream
    //       must not blank it.
    // Overlapping ids otherwise use the server version (streaming content is
    // committed on completion, so the server copy is authoritative once
    // streamingId clears). De-dup by id, then sort chronologically.
    setMessages((prev) => {
      const fetched = Array.isArray(msgs) ? msgs.filter(Boolean) : [];
      const streamId = streamingIdRef.current;

      // When switching sessions the backend may return an empty list
      // (e.g. chat_messages table is not yet populated for this
      // session). Don't keep stale messages from the previous session.
      if (fetched.length === 0 && !streamId) {
        return [];
      }

      const fetchedIds = new Set(fetched.map((m) => m.id));
      const currentSession = loadSessionRef.current;
      const preserved = (prev || []).filter((m) => {
        if (!m || !m.id) return false;
        // If the session changed since this fetch started, reject ALL
        // messages from the previous session — they don't belong here.
        if (currentSession !== sid) return false;
        // If a prev message carries a session_id, it must match the
        // current session. Messages from conv rehydration (raw LLM
        // format) lack session_id and rely on the loadSessionRef guard
        // above + the conv-rehydration functional-updater guard.
        if (m.session_id && m.session_id !== sid) return false;
        if (!fetchedIds.has(m.id)) return true;   // in-flight, not committed
        if (m.id === streamId) return true;        // streaming, local fresher
        return false;
      });
      const preservedIds = new Set(preserved.map((m) => m.id));
      const fetchedKept = fetched.filter((m) => !preservedIds.has(m.id));
      const merged = [...fetchedKept, ...preserved];
      merged.sort(
        (a, b) => new Date(a.created_date || 0) - new Date(b.created_date || 0),
      );
      // Inject persistent stream content that accumulated while Chat was
      // on a different page. The stream kept running via PersistentStreamContext
      // and buffered partial content — surface it in the last assistant message.
      try {
        const pending = stream.getPendingMessage(sid);
        if (pending && pending.content) {
          // Find the last assistant message and patch it with the buffer's
          // fresher content (partial content during streaming is always
          // newer than what was last committed to the DB).
          const lastAssistantIndex = merged.reduce(
            (best, m, i) => (m.role === 'assistant' ? i : best), -1
          );
          if (lastAssistantIndex >= 0) {
            const existing = merged[lastAssistantIndex];
            const bufLen = (pending.content || '').length;
            const existingLen = (existing.content || '').length;
            if (bufLen > existingLen) {
              merged[lastAssistantIndex] = {
                ...existing,
                content: pending.content,
                tool_calls: pending.toolCalls || existing.tool_calls || null,
                loading: pending.loading !== false && stream.isActiveForSession(sid),
              };
            }
          } else if (pending.loading !== false && stream.isActiveForSession(sid)) {
            // The assistant message was never committed to the DB (brand-new
            // generation started while Chat was unmounted). Inject a placeholder
            // so the user sees the partial content immediately on return.
            merged.push({
              id: pending.id || `pending-${Date.now()}`,
              role: 'assistant',
              content: pending.content,
              tool_calls: pending.toolCalls || null,
              loading: true,
              created_date: new Date().toISOString(),
            });
          }
        }
      } catch { /* provider may not be available */ }
      // Cross-store content-fingerprint dedup: conv.messages and chat_messages
      // share no id space, so the id-based merge above keeps both copies when
      // the same logical message was persisted to both stores. The fingerprint
      // intentionally does NOT include ``created_date`` — the two stores set
      // different timestamps for the same logical message (backend's
      // ``datetime.utcnow()`` vs the chat_messages row's DB default), so any
      // fingerprint that includes ``created_date`` would fail to match. First
      // occurrence wins: ``fetchedKept`` is at the head of ``merged``, so the
      // server-confirmed chat_messages copy is kept.
      return dedupeMessagesByFingerprint(merged);
    });
    setLoadStatus('loaded');
  }

  useEffect(() => {
    // One-time migration: drop any v1 `zhanlu:messages:${sid}` entries so
    // users with stale caches (post-cleanup of stale "Sorry" rows in the
    // backend) immediately see empty convs instead of the old error. Only
    // drops keys shaped like the v1 contract; v2 keys are untouched.
    try {
      const toRemove = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && /^zhanlu:messages:[^:]+$/.test(k)) toRemove.push(k);
      }
      toRemove.forEach((k) => { try { localStorage.removeItem(k); } catch { /* noop */ } });
    } catch { /* noop */ }
  }, []);

  // P2 mid-turn steer: post the current input to the in-flight v3 stream
  // without starting a new stream. Best-effort; a 429 (queue full) or 404
  // (conversation gone) surfaces a toast. On success the input is cleared.
  async function handleSteer(text) {
    const trimmed = (text || '').trim();
    if (!trimmed) return;
    if (!streamingConvId) {
      // No in-flight stream — fall through to the normal send path.
      return handleSend(trimmed);
    }
    setInputValue('');
    clearDraft('chat_input_draft');
    try {
      await steerAgentConversation(streamingConvId, trimmed);
    } catch (e) {
      toast.error(e?.message || 'Steer failed');
    }
  }

  async function handleSend(text, opts = {}) {
    const gen = ++genRef.current;
    const { hidden = false, newSessionTitle = null, newSessionProject = null, newSessionProjectId = null } = opts; // hidden=true → don't render the bubble
    // `newSession*` opts are used when the caller wants the message to
    // land in a brand-new session (e.g. from the New-Automation dialog)
    // rather than appending to the currently-active one. The session is
    // created and set active BEFORE the message is added so handleSend
    // can pick it up via `activeId` on the next render.
    let sid = activeId;
    let current = [...messages];
    const skill = activeSkill;
    // The selected skill is shown as a chip next to the input — we do
    // NOT need to echo ``/${skill}`` into the user-visible message text.
    // The previous behavior prepended the slash token to ``fullText``,
    // which made the user's own message read like
    // "/frontend-design how do I make a button?" — UX noise that also
    // confused the LLM into thinking the user issued a slash command
    // rather than asking a question. The skill's methodology is now
    // injected into the SYSTEM prompt (not the user turn) via
    // ``skillContext`` below.
    const fullText = text;
    // `newSessionTitle` doubles as the "force a brand-new session"
    // signal — even if the user already has an active session, we
    // create a fresh one. This is what the New-Automation dialog
    // relies on: each new task must land in its own clean chat
    // (Manus-style) regardless of which session the user was just in.
    const forceNewSession = Boolean(newSessionTitle);
    if (!sid || forceNewSession) {
      // Title resolution: caller may have passed `newSessionTitle`
      // (e.g. "新建任务" from the New-Automation dialog). Otherwise
      // fall back to a snippet of the user text.
      const titleSource = newSessionTitle || fullText;
      const session = await base44.entities.ChatSession.create({
        title: titleSource.slice(0, 24),
        // Store null (not the "Ungrouped" i18n string) for sessions
        // without a bound project. The "Ungrouped" label is purely
        // a UI display string for the sidebar's group header and
        // the project picker — it is NOT a stored value. Storing
        // the string here caused two bugs:
        //   1. The chat input showed a "Ungrouped" chip with an X
        //      button for the implicit default state, which is
        //      confusing because the user has nothing selected.
        //   2. Legacy rows that got the string written have to be
        //      normalized on read (see ChatSessionContext.
        //      selectSession) before they can be rendered
        //      correctly.
        // Existing legacy rows are handled by the read-side
        // normalization; new rows are clean.
        project: newSessionProject || pendingProject || null,
        // Set the FK too (in addition to the legacy name) so the
        // session shows up in the project's "Recent Chats" list and
        // any project-scoped queries pick it up. Both fields are
        // written so legacy readers keep working unchanged.
        ...(newSessionProjectId ? { project_id: newSessionProjectId } : (pendingProjectId ? { project_id: pendingProjectId } : {})),
        last_message_at: new Date().toISOString(),
      });
      sid = session.id;
      setActiveId(sid);
      prependSession(session);
    }
    const userMsg = await base44.entities.ChatMessage.create({
      session_id: sid, role: 'user', content: fullText, order: current.length,
      // Persist the attached files on the message so the file cards render
      // in chat history (Kimi/ChatGPT-style) and survive a page refresh.
      ...(attachments.length ? { attachments: attachments.map((a) => ({ name: a.name, file_url: a.file_url, ext: a.ext, kind: a.kind })) } : {}),
    });
    // Mark synthetic / auto-injected user messages so the renderer can
    // hide them. The text is still sent to the LLM (so it sees the
    // directive) but the chat bubble is suppressed.
    if (hidden) userMsg.hidden = true;
    current = [...current, userMsg];
    setMessages(current);
    setInputValue('');
    clearDraft('chat_input_draft');
    setActiveSkill(null);
    setAttachments([]);
    setLoading(true);
    // Single source of truth for the Stop button — must happen before any
    // await so the AbortController exists for the fetch / SSE call below.
    // (sessionId, gen): scope the active-stream state to THIS session so
    // other sessions' inputs stay interactive while this one streams.
    stream.startSending(sid, gen);
    // --- Chat without an explicit agent: ALWAYS auto-select
    // ``general_assistant`` so the conversation runs through the agent
    // runtime (which injects the date anchor, project KBs, memory,
    // and — critically — the real-time tools ``web_search``,
    // ``web_extract``, and ``agent_browser``). This applies to EVERY
    // chat that has no user-picked agent, including Ungrouped chats
    // with no project. The previous condition ``&& pendingProjectId``
    // meant Ungrouped chats silently fell through to the
    // ``InvokeLLMStream`` proxy, which has no tools and no date
    // anchor — so the LLM would answer with stale training data for
    // simple questions like "what's today's date". Removing the gate
    // makes every chat get the full agent runtime, which is the
    // user-expected behavior (consistent with what the user already
    // gets on any project).
    //
    // IMPORTANT: we use ``effectiveAgent`` only as a local handle for
    // routing the request — we do NOT call ``setActiveAgent(...)`` on
    // the auto-resolved agent. ``general_assistant`` is a platform
    // system agent and is hidden from the UI (chip, agent picker, My
    // Space), so persisting it to the active-agent state would cause
    // the user to see "general_assistant" pinned in their input bar
    // even though they never picked it. The next send runs the
    // auto-select again, so the silent resolution is stable across
    // multiple messages.
    let effectiveAgent = activeAgent;
    if (!effectiveAgent) {
      try {
        const _agents = await base44.entities.AgentApp.list();
        // Fallback: general_assistant for all projects or if no agent is
        // found.
        if (!effectiveAgent) {
          effectiveAgent = _agents.find((a) => a.name === 'general_assistant') || null;
        }
        // Deliberately do NOT setActiveAgent here — see comment above.
        // Fallback: if the list endpoint didn't return general_assistant
        // (e.g. backend owner-scoping excludes system agents with
        // created_by_id=NULL), use a minimal agent object so the full
        // agent runtime path (tools, memory, web search) is still taken.
        // The backend resolves AgentApp by name, so name is sufficient.
        if (!effectiveAgent) {
          effectiveAgent = { name: 'general_assistant' };
        }
      } catch { /* fall through to generic path */ }
    }
    // --- Automation-origin chats: bind to the dedicated
    // ``automation_agent`` instead of the legacy non-agent path. The
    // automation_agent has the focused toolset (create_automation,
    // update_automation, execute_automation, clarify, list_data_sources)
    // and a strict system prompt that (a) mandates [[CLARIFY]]
    // single-select option cards for disambiguation, and (b) treats a
    // data-source answer as a configuration binding — never as a report
    // request. This fixes the bug where selecting a data source in an
    // automation chat triggered report generation instead of binding
    // the source to the task.
    //
    // This applies to:
    //   1. New chats started from the Automation dialog — the prefill
    //      matches ``isCreateAutomationRequest``.
    //   2. Existing automation-bound chats — ``activeAgent`` is already
    //      ``automation_agent`` (set by conv rehydration).
    // Other chats (general, project-scoped, custom agents) keep their
    // current agent. We do NOT ``setActiveAgent`` here — the binding
    // persists via the conversation's ``agent_name`` field, which the
    // rehydration effect reads on reload.
    const isAutomationOrigin = isCreateAutomationRequest(fullText)
      || (activeAgent && activeAgent.name === 'automation_agent');
    if (isAutomationOrigin && (!effectiveAgent || effectiveAgent.name !== 'automation_agent')) {
      try {
        const _autoAgents = await base44.entities.AgentApp.list();
        const autoAgent = _autoAgents.find((a) => a.name === 'automation_agent');
        if (autoAgent) {
          effectiveAgent = autoAgent;
        } else {
          // Fallback: minimal agent object — the backend resolves
          // AgentApp by name, so name is sufficient for the v3 runtime
          // to load the correct system prompt + tools.
          effectiveAgent = { name: 'automation_agent' };
        }
      } catch { /* keep whatever agent was resolved */ }
    }
    // --- P2 mid-turn steer: if a v3 agent stream is already in flight,
    // route the message through the steer endpoint instead of starting a
    // new stream. Falls through to the normal agent path otherwise.
    if (effectiveAgent && stream.isActiveForSession(activeId) && streamingConvId) {
      return handleSteer(fullText);
    }
    // --- Agent mode: route through the v3 conversations endpoint so the
    // backend can run the full agent loop (system prompt, memory, skills,
    // ``prepare_data_source_runtime`` for the bound KnowledgeBases,
    // tool calling, guardrails). The previous flow used
    // ``InvokeLLMStream`` which is a raw LLM proxy with no tools.
    if (effectiveAgent) {
      const fileUrls = attachments.map((a) => a.file_url);
      const aiMsg = await base44.entities.ChatMessage.create({
        session_id: sid, role: 'assistant', content: '', order: current.length,
      });
      if (gen !== genRef.current) {
        // A newer generation took over before we even started streaming.
        stream.resetStream();
        return;
      }
      setMessages((prev) => [...prev, aiMsg]);
      setStreamingId(aiMsg.id);
      setLoading(false);
      // P2 (conversational dashboards): fresh message → fresh dedupe count.
      dashboardSeenRef.current.set(aiMsg.id, 0);
      try {
        const effectiveAiLang = aiLang === 'auto' ? (detectLang(fullText) || 'zh') : aiLang;
        // Resolve the conv id to reuse. Source of truth, in priority
        // order:
        //   1. streamingConvId — set inside handleAgentSend after
        //      the first create. Synchronous for same-session
        //      follow-up sends (React state is updated by the time
        //      the user types the next message).
        //   2. The active ChatSession's conversation_id — written
        //      at the end of the first createAgentConversation()
        //      call. Persists across page reloads (assuming the
        //      write landed before the reload).
        //   3. The URL's ?conv= param — most-recent source, lets
        //      the user reload mid-stream and continue.
        //
        // If all three are missing, handleAgentSend will lazily
        // create a new AgentConversation.
        let resumeConvId = streamingConvId;
        if (!resumeConvId && sid) {
          const sess = getSession ? getSession(sid) : null;
          resumeConvId = sess?.conversation_id || null;
        }
        if (!resumeConvId) {
          try {
            const urlConv = new URLSearchParams(window.location.search).get('conv');
            if (urlConv) resumeConvId = urlConv;
          } catch { /* SSR / test env */ }
        }
        await handleAgentSend(fullText, {
          activeAgent: effectiveAgent,
          activeSkill: skill,
          sid,
          gen,
          sessionId: resumeConvId,
          aiMsg,
          fileUrls,
          lang: effectiveAiLang,
        });
      } finally {
        if (gen === genRef.current) {
          setLoading(false);
          setStreamingId(null);
          // Always drain the stream state when this generation finishes
          // its work, otherwise the Stop button stays visible.
          if (stream.isActive) stream.complete();
        }
      }
      return;
    }
    try {
      // AI output language: controlled by separate aiLang setting (decoupled from UI language).
      //   'auto' → detect language from user input, fallback to Chinese
      //   'zh'   → always respond in Chinese
      //   'en'   → always respond in English
      const effectiveAiLang = aiLang === 'auto' ? (detectLang(fullText) || 'zh') : aiLang;
      const langInstruction = effectiveAiLang === 'en'
        ? 'Always respond in English.'
        : '请使用中文回复。';
      const historyLabelUser = effectiveAiLang === 'en' ? 'User' : '用户';
      const historyLabelAssistant = effectiveAiLang === 'en' ? 'Assistant' : '助手';
      const history = current.map((m) => `${m.role === 'user' ? historyLabelUser : historyLabelAssistant}: ${m.content}`).join('\n');
      // Skill context is built directly from the selected skill OBJECT
      // (``activeSkill`` is now the full Tool row, not a string token).
      // This eliminates the previous fragile lookup
      // (``tools.find(t => t.trigger?.includes(skill) || t.name?.toLowerCase().includes(skill))``
      // which returned the wrong skill for short tokens like "pdf"
      // matching "PDF Generator" first). The user picked a specific
      // skill row; we use it verbatim.
      // Skill context: custom-skill methodology takes priority.
      // When no custom skill is active, inject the default-skills hint
      // so the LLM knows the 6 built-in artifact skills are available.
      const skillContext = buildSkillContext(skill);
      const defaultSkillContext = buildDefaultSkillContext(skill);
      let agentContext = '';
      if (activeAgent) {
        const topoMap = { standalone: '独立运行', sequence: '顺序协作', loop: '循环协作', parallel: '并行协作' };
        const parts = [`当前已加载智能体「${activeAgent.name}」。`, `描述：${activeAgent.description || ''}`];
        if (activeAgent.agent_type) parts.push(`智能体类型：${{ sequential: '顺序型', reactive: '反应型', deliberative: '慎思型' }[activeAgent.agent_type] || activeAgent.agent_type}`);
        if (activeAgent.capabilities?.length) parts.push(`工具：${activeAgent.capabilities.join('、')}`);
        const layers = [
          ['角色定位', activeAgent.prompt_identity], ['边界约束', activeAgent.prompt_boundary],
          ['推理流程', activeAgent.prompt_reasoning], ['职能与工具', activeAgent.prompt_tools], ['输出规范', activeAgent.prompt_output],
        ].filter(([, v]) => v && v.trim());
        if (layers.length) {
          parts.push('【五层宪法提示词】');
          layers.forEach(([label, val]) => parts.push(`【${label}】\n${val}`));
        } else if (activeAgent.system_prompt) {
          parts.push(`核心指令：${activeAgent.system_prompt}`);
        }
        if (activeAgent.skills?.length) {
          parts.push(`可用技能：${activeAgent.skills.join('、')}`);
          // Equipped skills need their full methodology (skill_md body) in
          // the system prompt — otherwise the LLM only sees the names and
          // has no idea what those skills DO. AgentApp.skills is a JSON
          // array of skill NAMES (not ids), so we look them up by name.
          // Lookup is case-insensitive + whitespace-trimmed to handle
          // trivial drift between the config form and the tool row.
          try {
            const toolRows = await base44.entities.Tool.list();
            const wanted = new Set(
              (activeAgent.skills || []).map((s) => (s || '').trim().toLowerCase()).filter(Boolean)
            );
            // If a row's name matches any equipped name (and there may be
            // multiple rows with the same name — keep the one with the
            // longest skill_md so the LLM gets the richest body).
            // Dedupe by lowercase name so the LLM doesn't see two
            // "writing-plans" blocks with conflicting methodologies.
            const equipped = toolRows
              .filter((r) => wanted.has((r.name || '').trim().toLowerCase()))
              .sort((a, b) => (b.skill_md?.length || 0) - (a.skill_md?.length || 0));
            const seen = new Set();
            const equippedUnique = equipped.filter((r) => {
              const k = (r.name || '').trim().toLowerCase();
              if (seen.has(k)) return false;
              seen.add(k);
              return true;
            });
            if (equippedUnique.length) {
              parts.push('【已装载技能 - 严格遵循以下方法论执行】');
              for (const r of equippedUnique) {
                parts.push(`### 技能: ${r.name}`);
                if (r.description) parts.push(`描述: ${r.description}`);
                if (r.trigger) parts.push(`触发词: ${r.trigger}`);
                if (r.skill_md && String(r.skill_md).trim()) {
                  parts.push(`方法论:\n${r.skill_md}`);
                } else {
                  parts.push('(无方法论正文 - 仅按名称识别，可按描述自主推断)');
                }
              }
            }
          } catch { /* noop — the names-only line above still stands as a fallback */ }
        }
        if (activeAgent.knowledge_bases?.length) {
          try {
            const allKbs = await base44.entities.KnowledgeBase.list();
            const allowed = allKbs.filter((kb) => activeAgent.knowledge_bases.includes(kb.id));
            if (allowed.length) {
              parts.push('【可访问数据源（仅限以下知识库，严禁访问未列出的数据库）】');
              allowed.forEach((kb) => {
                const dbLabel = t.kb.dbTypes[kb.db_type] || kb.db_type || 'DB';
                const loc = kb.host || kb.api_url || '';
                parts.push(`• ${kb.name}（${dbLabel}）：${loc}${kb.database_name ? `/${kb.database_name}` : ''}`);
              });
            }
          } catch { /* noop */ }
        }
        if (activeAgent.topology && activeAgent.topology !== 'standalone') {
          parts.push(`协作拓扑：${topoMap[activeAgent.topology] || activeAgent.topology}`);
          if (activeAgent.sub_agents?.length) parts.push(`委派子 Agent：${activeAgent.sub_agents.join('、')}`);
        }
        parts.push(`控制约束：调用上限${activeAgent.max_call_count ?? 50}次，重试${activeAgent.max_retries ?? 3}次，数据${activeAgent.data_read ? '可读' : '禁读'}${activeAgent.data_write ? '/可写' : '/禁写'}${activeAgent.human_fallback ? '，高风险转人工' : ''}`);
        if (activeAgent.trace_enabled) {
          const lvl = activeAgent.log_level || 'info';
          const lvlHint = lvl === 'debug' ? '记录全部细节：思考链全文、工具完整入参出参' : lvl === 'info' ? '记录关键节点：每步标题与要点' : lvl === 'warn' ? '仅记录警告及以上步骤' : '仅记录异常步骤';
          parts.push(`可追溯：已启用全链路埋点（日志级别 ${lvl}，${lvlHint}）。请在 trace 字段输出 3-6 个高层执行节点，记录目标分析、工具或数据操作、验证与输出组装；detail 仅说明做了什么和得到什么结果，不输出隐藏思维链。每项含 step、type、title、detail、status、duration_ms。`);
        }
        parts.push('请以该智能体的身份、能力边界与推理框架回复用户。主动规划并连续完成多步骤任务；先给成果，再给简洁依据；仅在缺少关键信息或高风险操作时询问用户，并在输出前核验事实、数据与工具结果。');
        agentContext = `\n${parts.join('\n')}`;
      }
      // --- Deterministic intent classification (no LLM call) ---
      const intent = classifyIntent(fullText);
      const intentHint = formatHint(intent);

      let automationContext = '';
      // NOTE: automation-origin chats now bind to the real
      // ``automation_agent`` (see the isAutomationOrigin block above)
      // and route through the agent path — they never reach this
      // legacy non-agent branch. The ``AUTOMATION_AGENT_PROMPT`` overlay
      // below is kept for backward compatibility only (any chat that
      // still falls into the non-agent path with a create-automation
      // intent). The backend automation_agent has its own system prompt
      // with the [[CLARIFY]] protocol + answer-binding rules, so the
      // overlay is no longer the primary automation prompt.
      if (!activeAgent) {
        // The ``AUTOMATION_AGENT_PROMPT`` data-source list is only
        // useful for CREATE flows (the LLM needs to know which DBs are
        // available to bind the new task to). For RUN flows the agent
        // has ``execute_automation`` and doesn't need the data-source
        // catalog injected here.
        const hasCreateAutomation = current.some((m) => m.role === 'user' && isCreateAutomationRequest(m.content));
        if (isCreateAutomationRequest(fullText) || hasCreateAutomation) {
          try {
            const kbs = await base44.entities.KnowledgeBase.list();
            const dbSources = kbs.filter((kb) => kb.source_kind === 'database' || kb.type === 'business_db');
            const sourceList = dbSources.length
              ? dbSources.map((kb) => `• ${kb.name}（${t.kb.dbTypes[kb.db_type] || kb.db_type || 'DB'}）`).join('\n')
              : '（暂无已配置的数据库连接，可稍后在详情页配置）';
            automationContext = `\n${AUTOMATION_AGENT_PROMPT}\n\n【可用数据源】\n${sourceList}`;
          } catch {
            automationContext = `\n${AUTOMATION_AGENT_PROMPT}`;
          }
        }
      }
      const systemPrompt = `${baseSystemPrompt}${skillContext}${defaultSkillContext}${agentContext}${automationContext}\n${langInstruction}${intentHint ? `\n\n【当前意图检测】${intentHint}` : ''}`;
      const fileUrls = attachments.map((a) => a.file_url);
      const fileNote = fileUrls.length ? `\n用户附带了 ${fileUrls.length} 个文件，请结合文件内容回复。` : '';
      // Per-request schema — makes `create_resource` required when the
      // user is asking to create an automation task. This is the
      // strongest signal we can give the LLM that the response MUST
      // contain the field, preventing it from going off to file-writing
      // or code-execution tools (which always fail in this app).
      const responseSchema = buildResponseSchema(fullText, current);
      // --- Create AI message placeholder for real-time streaming ---
      const aiMsg = await base44.entities.ChatMessage.create({
        session_id: sid, role: 'assistant', content: '', order: current.length,
      });
      if (gen !== genRef.current) {
        // A newer generation took over before SSE started. Only drain the
        // shared stream state if no newer stream began (e.g. a session
        // switch / clear bump). If a newer stream started, it owns the
        // state — calling resetStream() here would abort ITS controller.
        if (stream.streamGenRef.current === gen) stream.resetStream();
        return;
      }
      setMessages((prev) => [...prev, aiMsg]);
      setStreamingId(aiMsg.id);
      setLoading(false);
      // P2 (conversational dashboards): fresh message → fresh dedupe count.
      dashboardSeenRef.current.set(aiMsg.id, 0);

      // --- Stream LLM response in real-time via SSE ---
      let fullContent = '';
      let aborted = false;
      try {
        stream.startSending(sid, gen);
        const streamRes = await authFetch(`/api/apps/${appParams.appId}/integration-endpoints/Core/InvokeLLMStream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt: `${systemPrompt}\n\n对话历史:\n${history}${fileNote}\n\n请回复:`,
            response_json_schema: responseSchema,
            model: 'gpt_5_4',
            ...(fileUrls.length ? { file_urls: fileUrls } : {}),
          }),
          signal: stream.abortRef.current?.signal,
        });
        if (!streamRes.ok) throw new Error(`Stream failed: ${streamRes.status}`);
        const reader = streamRes.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          if (gen !== genRef.current) return;
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const evt = JSON.parse(line.slice(6));
              if (evt.error) throw new Error(evt.error);
              if (evt.delta) {
                fullContent += evt.delta;
                const partial = extractPartialText(fullContent);
                if (partial && gen === genRef.current) {
                  setMessages((prev) => prev.map((m) => (m.id === aiMsg.id ? { ...m, content: partial } : m)));
                  // Persist partial content so it survives page navigation
                  stream.updatePendingMessage(sid, { id: aiMsg.id, content: partial, loading: true });
                }
              }
              if (evt.done) fullContent = evt.content;
            } catch (parseErr) {
              if (parseErr && parseErr.message && !parseErr.message.includes('JSON')) throw parseErr;
            }
          }
        }
        // Normal end-of-stream — clear the in-flight flag so the
        // Stop button disappears immediately and the user can send again.
        if (gen === genRef.current) {
          // Finalize the pending buffer with loading: false so the
          // next loadMessages doesn't show a stale loading indicator.
          stream.updatePendingMessage(sid, { id: null, content: partial, loading: false });
          stream.complete();
        }
      } catch (streamErr) {
        // AbortError: user clicked Stop. Mark aborted so we don't run the
        // non-streaming fallback and append a "Stopped generating." hint.
        if (streamErr && (streamErr.name === 'AbortError' || streamErr.code === 20)) {
          aborted = true;
          if (gen === genRef.current) {
            const stoppedText = lang === 'en' ? '\n\n[Stopped generating.]' : '\n\n[已停止生成。]';
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, content: (m.content || '') + stoppedText } : m
            ));
            // Stop button should disappear immediately when the user cancels
            stream.clearPendingMessage(sid);  // clear buffer on abort
            stream.complete();
          }
          return;
        }
        // Fallback to non-streaming InvokeLLM if SSE fails
        if (gen !== genRef.current) return;
        const res = await base44.integrations.Core.InvokeLLM({
          prompt: `${systemPrompt}\n\n对话历史:\n${history}${fileNote}\n\n请回复:`,
          response_json_schema: responseSchema,
          model: 'gpt_5_4',
          ...(fileUrls.length ? { file_urls: fileUrls } : {}),
        });
        const resData = (res && typeof res === 'object' ? res : {}) || {};
        fullContent = resData.text ? JSON.stringify(resData) : (resData.response ? JSON.stringify(resData.response) : JSON.stringify(resData));
        if (gen === genRef.current) {
          setMessages((prev) => prev.map((m) => (m.id === aiMsg.id ? { ...m, content: resData.text || resData.response || '...' } : m)));
        }
        // The fallback path never went through `stream.complete()`, so
        // the input bar can stay stuck on "responding" even though the user
        // already got a reply. Flip the state now — the outer `finally` will
        // also do this, but doing it here keeps the indicator honest if any
        // later step throws before the finally runs.
        stream.clearPendingMessage(sid);  // clear buffer on error fallback
        // Guard with gen so a stale generation can't clear a newer
        // session's active stream state.
        if (gen === genRef.current && stream.isActive) stream.complete();
      }

      // --- Parse complete JSON response ---
      let data = {};
      try {
        data = JSON.parse(fullContent);
        if (typeof data !== 'object' || data === null) data = { text: String(fullContent) };
      } catch {
        data = { text: fullContent };
      }

      let reply = data.text || '';
      // Hoisted entity map so the auto-retry branch below can reuse it
      // when the first response missed `create_resource`.
      const ENTITY_MAP = { report: 'Report', file: 'UserFile', agent: 'AgentApp', kb: 'KnowledgeBase', automation: 'AutomationTask', flow: 'DecisionFlow' };
      if (data.create_resource && data.create_resource.type) {
        const cr = data.create_resource;
        let parsedFields = {};
        try { parsedFields = typeof cr.fields === 'string' ? JSON.parse(cr.fields) : (cr.fields || {}); } catch { parsedFields = {}; }
        try {
          if (cr.type === 'file' && ['html', 'htm'].includes((parsedFields.file_type || '').toLowerCase()) && parsedFields.html_content) {
            const fileName = /\.html?$/i.test(cr.name) ? cr.name : `${cr.name}.html`;
            const htmlFile = new File([parsedFields.html_content], fileName, { type: 'text/html' });
            const uploaded = await base44.integrations.Core.UploadFile({ file: htmlFile });
            parsedFields = { ...parsedFields, file_url: uploaded.file_url, source: 'ai_generated', resource_kind: 'html_file' };
            delete parsedFields.html_content;
          }
          const entityName = ENTITY_MAP[cr.type];
          if (entityName) {
            // Stamp every non-report resource with the originating
            // session id when we have one. This used to be limited to
            // `file` resources, but the sidebar now uses
            // `AutomationTask.session_id` to render a small clock
            // icon on the originating chat session so users can
            // easily find the "control room" for an automation.
            // `report` keeps its own { title, ... } shape; reports
            // don't currently surface a per-session badge.
            // Prefer the LLM-parsed project (from the create-dialog's
            // "- Project：X" prefill) over the ambient chat chip, so an
            // explicit dialog selection isn't silently overwritten with
            // "Ungrouped" when no chip is set. See lib/createResourceProject.js.
            const projectFallback = pickCreateProject(parsedFields, pendingProject, t.sessionList.ungrouped);
            // BUGFIX (project binding): pass `project_id` (UUID FK) in addition
            // to the legacy `project` string so the executor's
            // `_resolve_task_project` finds the right project (and binds the
            // project's data sources) when the task runs. Without this, data
            // sources bound to the project silently fall back to a
            // different project and the report comes back empty.
            const projectIdFk = pendingProjectId || null;
            const payload = cr.type === 'report'
              ? { title: cr.name, ...parsedFields, ...(sid ? { session_id: sid, project: projectFallback, ...(projectIdFk ? { project_id: projectIdFk } : {}), agent_name: effectiveAgent?.name || null } : {}) }
              : { name: cr.name, ...parsedFields, ...(sid ? { session_id: sid, project: projectFallback, ...(projectIdFk ? { project_id: projectIdFk } : {}) } : {}) };
            const created = await base44.entities[entityName].create(payload);
            // Manus-style: when an automation is created from a chat, rename
            // the current session to the automation's name so the sidebar
            // shows "Inventory Anomaly Inspection" (not "新建任务" / the first
            // 24 chars of the user's message). We do THREE things to make
            // the rename visible in the sidebar no matter how it's rendered:
            //   1. optimistic context update via renameSession (instant)
            //   2. direct backend update of ChatSession.title (persistence)
            //   3. refreshSessions() to force the sidebar to re-read the
            //      server truth (covers the case where the optimistic
            //      update doesn't reach the sidebar in time, e.g. when a
            //      fresh session was just prependSession'd in the same tick).
            if (cr.type === 'automation' && sid && cr.name) {
              try {
                if (renameSession) renameSession(sid, cr.name);
                await base44.entities.ChatSession.update(sid, { title: cr.name });
                if (refreshSessions) refreshSessions();
              } catch { /* non-fatal — sidebar will catch up on next refresh */ }
            }
            // Notify other mount points (SessionList) that a new automation
            // exists so they refresh their automation-derived UI (e.g. the
            // Clock badge on the originating chat session row). Without this,
            // the badge only appears on the next page load or navigation.
            if (cr.type === 'automation') {
              try {
                window.dispatchEvent(new CustomEvent('zhanlu:automation-created', {
                  detail: { id: created && created.id, name: cr.name, session_id: sid },
                }));
              } catch { /* non-fatal */ }
            }
            let docxUrl = null;
            let pptxUrl = null;
            if (cr.type === 'report') {
              try {
                const res = await base44.functions.invoke('generateReportDocx', { title: cr.name, markdown: parsedFields.summary || '', sessionId: sid, project: pendingProject || t.sessionList.ungrouped });
                docxUrl = res.data?.file_url || res?.file_url;
                if (docxUrl) await base44.entities.Report.update(created.id, { file_url: docxUrl });
              } catch { /* DOCX generation optional */ }
            }
            if (cr.type === 'file' && parsedFields.file_type === 'pptx' && Array.isArray(parsedFields.slides)) {
              try {
                const res = await base44.functions.invoke('generatePptx', { title: cr.name, slides: parsedFields.slides });
                pptxUrl = res.data?.file_url || res?.file_url;
                if (pptxUrl) await base44.entities.UserFile.update(created.id, { file_url: pptxUrl });
              } catch { /* PPTX generation optional */ }
            }
            reply += `${reply ? '\n\n' : ''}[[RESULT]]\n${JSON.stringify({ type: cr.type, id: created.id, name: cr.name, fields: parsedFields, draft: false, ...(parsedFields.file_url ? { file_url: parsedFields.file_url } : {}), ...(docxUrl ? { file_url: docxUrl } : {}), ...(pptxUrl ? { file_url: pptxUrl } : {}) })}\n[[END]]`;
          }
        } catch {
          reply += `${reply ? '\n\n' : ''}[[RESULT]]\n${JSON.stringify({ type: cr.type, name: cr.name, fields: parsedFields, draft: true })}\n[[END]]`;
        }
      }
      // The agent has now either created the resource (cr.type === 'automation'
      // and we got an id back) or saved a draft. Either way, the
      // editable draft card has served its purpose — dismiss it so
      // the chat focuses on the result card.
      if (data.create_resource && data.create_resource.type === 'automation') {
        setPendingDraft(null);
      }
      // Auto-retry guard: if the user asked to create an automation
      // task but the LLM failed to output `create_resource` (it went
      // to file/code tools instead), inject a corrective follow-up and
      // make ONE more call. If that also fails, surface the error to
      // the draft card so the user can hit Retry themselves. Only
      // applies to CREATE flows — RUN flows use the agent's
      // ``execute_automation`` tool and have no ``create_resource``
      // requirement.
      //
      // NOTE: automation-origin chats that bind to the real
      // ``automation_agent`` (see the isAutomationOrigin block above)
      // route through the agent path and never reach this legacy
      // non-agent retry guard — the agent's ``create_automation`` tool
      // handles creation server-side. This guard only fires for chats
      // that still fall into the non-agent path.
      const askedForAutomation = isCreateAutomationRequest(fullText)
        || (Array.isArray(current) && current.some((m) => m?.role === 'user' && isCreateAutomationRequest(m?.content)));
      const resourceCreated = !!(data.create_resource && data.create_resource.type);
      if (askedForAutomation && !resourceCreated && gen === genRef.current) {
        try {
          // Persist the failed attempt so the user can see what the
          // agent said before we retry.
          const failedReply = reply || data.text || JSON.stringify(data);
          await base44.entities.ChatMessage.update(aiMsg.id, { content: failedReply, ...(trace ? { trace } : {}) });
          setMessages((prev) => prev.map((m) => (m.id === aiMsg.id ? { ...m, content: failedReply, ...(trace ? { trace } : {}) } : m)));
          // Inject a corrective user message + a system note in the
          // conversation history, then re-run the LLM with an extra-
          // strict schema that requires `create_resource` and bans all
          // tool use in the prompt.
          const systemNote = effectiveAiLang === 'en'
            ? '[System] Your previous reply did not include a `create_resource` field, so the automation was not created. Reply again with ONLY a JSON object that contains `text` and `create_resource: { type: "automation", name, fields }`. Do NOT use any file, code-execution, or shell tool — they all fail in this app.'
            : '[系统] 你上一条回复没有包含 `create_resource` 字段，因此自动化任务没有被创建。请重新回复，只输出一个 JSON 对象，包含 `text` 和 `create_resource: { type: "automation", name, fields }`。严禁使用任何文件、代码执行、shell 工具——它们在此应用中全部不可用。';
          const correctiveMsg = await base44.entities.ChatMessage.create({
            session_id: sid, role: 'user', content: systemNote, order: current.length + 1,
          });
          setMessages((prev) => [...prev, correctiveMsg]);
          // Re-run with a tighter schema. The system prompt already
          // contains the strict "do not use file tools" language, but
          // we append an extra reminder to the user-side prompt.
          const retryAiMsg = await base44.entities.ChatMessage.create({
            session_id: sid, role: 'assistant', content: '', order: current.length + 2,
          });
          setMessages((prev) => [...prev, retryAiMsg]);
          setStreamingId(retryAiMsg.id);
          const retryCurrent = [...current, aiMsg, correctiveMsg];
          const retryHistory = retryCurrent.map((m) => `${m.role === 'user' ? historyLabelUser : historyLabelAssistant}: ${m.content}`).join('\n');
          const retryRes = await authFetch(`/api/apps/${appParams.appId}/integration-endpoints/Core/InvokeLLMStream`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              prompt: `${systemPrompt}\n\n对话历史:\n${retryHistory}${fileNote}\n\n请回复:`,
              response_json_schema: responseSchema,
              model: 'gpt_5_4',
              ...(fileUrls.length ? { file_urls: fileUrls } : {}),
            }),
            signal: stream.abortRef.current?.signal,
          });
          if (!retryRes.ok) throw new Error(`Retry stream failed: ${retryRes.status}`);
          // Reuse the same streaming pipeline (parse events, accumulate
          // buffer, extract final JSON). The streaming block above is
          // too tangled to factor out cleanly, so we re-do the parse
          // and creation here.
          const retryReader = retryRes.body.getReader();
          const retryDecoder = new TextDecoder();
          let retryBuffer = '';
          let retryFull = '';
          while (true) {
            if (gen !== genRef.current) break;
            const { done, value } = await retryReader.read();
            if (done) break;
            retryBuffer += retryDecoder.decode(value, { stream: true });
            const lines = retryBuffer.split('\n');
            retryBuffer = lines.pop();
            for (const line of lines) {
              if (!line.startsWith('data: ')) continue;
              try {
                const evt = JSON.parse(line.slice(6));
                if (evt.error) throw new Error(evt.error);
                if (evt.delta) retryFull += evt.delta;
                if (evt.done && typeof evt.response === 'string') retryFull = evt.response;
              } catch (err) { if (err.message && err.message !== 'Unexpected end of JSON input') throw err; }
            }
          }
          let retryData = {};
          try { retryData = JSON.parse(retryFull || '{}'); } catch { retryData = { text: retryFull }; }
          let retryReplyText = retryData.text || '';
          if (retryData.create_resource && retryData.create_resource.type) {
            const cr2 = retryData.create_resource;
            let pf = {};
            try { pf = typeof cr2.fields === 'string' ? JSON.parse(cr2.fields) : (cr2.fields || {}); } catch { pf = {}; }
            try {
              const e2 = ENTITY_MAP[cr2.type];
              if (e2) {
                const projectFallback2 = pickCreateProject(pf, pendingProject, t.sessionList.ungrouped);
                // BUGFIX (project binding): mirror the main path — include
                // `project_id` (UUID FK) alongside the legacy `project` string
                // so the executor can resolve the task's project and bind the
                // project's data sources on run.
                const projectIdFk2 = pendingProjectId || null;
                const payload2 = cr2.type === 'report' ? { title: cr2.name, ...pf, ...(sid ? { session_id: sid, project: projectFallback2, ...(projectIdFk2 ? { project_id: projectIdFk2 } : {}), agent_name: effectiveAgent?.name || null } : {}) } : { name: cr2.name, ...pf, ...(sid ? { session_id: sid, project: projectFallback2, ...(projectIdFk2 ? { project_id: projectIdFk2 } : {}) } : {}) };
                const created2 = await base44.entities[e2].create(payload2);
                // Same bulletproof rename as the main create_resource path
                // (see comment there). The retry fires when the agent's
                // first response lacked create_resource, so the rename is
                // essential here too.
                if (cr2.type === 'automation' && sid && cr2.name) {
                  try {
                    if (renameSession) renameSession(sid, cr2.name);
                    await base44.entities.ChatSession.update(sid, { title: cr2.name });
                    if (refreshSessions) refreshSessions();
                  } catch { /* non-fatal */ }
                }
                retryReplyText += `${retryReplyText ? '\n\n' : ''}[[RESULT]]\n${JSON.stringify({ type: cr2.type, id: created2.id, name: cr2.name, fields: pf, draft: false })}\n[[END]]`;
                if (cr2.type === 'automation') {
                  setPendingDraft(null);
                  try {
                    window.dispatchEvent(new CustomEvent('zhanlu:automation-created', {
                      detail: { id: created2 && created2.id, name: cr2.name, session_id: sid },
                    }));
                  } catch { /* non-fatal */ }
                }
              }
            } catch {
              retryReplyText += `${retryReplyText ? '\n\n' : ''}[[RESULT]]\n${JSON.stringify({ type: cr2.type, name: cr2.name, fields: pf, draft: true })}\n[[END]]`;
            }
          }
          if (!retryReplyText.trim()) retryReplyText = JSON.stringify(retryData);
          const retryTrace = Array.isArray(retryData.trace) ? retryData.trace : null;
          if (gen !== genRef.current) return;
          setMessages((prev) => prev.map((m) => (m.id === retryAiMsg.id ? { ...m, content: retryReplyText, ...(retryTrace ? { trace: retryTrace } : {}) } : m)));
          await base44.entities.ChatMessage.update(retryAiMsg.id, { content: retryReplyText, ...(retryTrace ? { trace: retryTrace } : {}) });
          // If the retry STILL didn't create the resource, surface
          // the error in the draft card so the user can manually retry.
          const stillMissing = !!(retryData.create_resource && retryData.create_resource.type);
          if (askedForAutomation && !stillMissing) {
            setPendingDraft((d) => d ? { ...d, error: effectiveAiLang === 'en' ? 'The agent could not create the task. Please retry or edit the values.' : 'Agent 暂时无法创建任务，请重试或调整内容。' } : d);
          }
        } catch (retryErr) {
          // Give up gracefully — leave the draft card open and let
          // the user retry from the UI.
          if (gen === genRef.current) {
            setPendingDraft((d) => d ? { ...d, error: effectiveAiLang === 'en' ? 'The agent could not create the task. Please retry or edit the values.' : 'Agent 暂时无法创建任务，请重试或调整内容。' } : d);
          }
        }
        setStreamingId(null);
        await base44.entities.ChatSession.update(sid, { last_message_at: new Date().toISOString() });
        touchSession(sid);
        return;
      }
      if (!reply.trim()) reply = JSON.stringify(data);
      const trace = Array.isArray(data.trace) && data.trace.length > 0 ? data.trace : null;
      if (gen !== genRef.current) return;
      setMessages((prev) => prev.map((m) => (m.id === aiMsg.id ? { ...m, content: reply, ...(trace ? { trace } : {}) } : m)));
      setStreamingId(null);
      onRunComplete(aiMsg.id, reply);
      await base44.entities.ChatMessage.update(aiMsg.id, { content: reply, ...(trace ? { trace } : {}) });
      await base44.entities.ChatSession.update(sid, { last_message_at: new Date().toISOString() });
      touchSession(sid);
    } catch (e) {
      if (gen !== genRef.current) return;
      // Bug 1 fix (applied to legacy handleSend path): don't surface raw
      // error text in the chat. Show a clean, localized message instead.
      // The raw error is still available in the browser console for debugging.
      console.error('[Chat stream error]', e);
      const friendlyErr = lang === 'en'
        ? "Sorry, I hit an error while responding. Please try again."
        : "抱歉，回复时出错了，请重试。";
      const errMsg = await base44.entities.ChatMessage.create({
        session_id: sid, role: 'assistant', content: friendlyErr, order: current.length,
      });
      if (gen !== genRef.current) return;
      setMessages((prev) => [...prev, errMsg]);
      onRunComplete(errMsg.id, errMsg.content || '');
      if (gen === genRef.current) stream.complete();
    } finally {
      if (gen === genRef.current) {
        setLoading(false);
        setStreamingId(null);
        // Defensive: ensure the stream state is always drained, even on
        // unexpected early returns. complete() is a no-op if already done.
        if (stream.isActive) stream.complete();
      }
    }
  }

  // -------------------------------------------------------------------------
  // Agent-mode send: routes through /agents/conversations/v3/.../stream so
  // the backend runs the full agent loop (system prompt, memory, skill
  // prompts, ``prepare_data_source_runtime`` for the agent's bound
  // KnowledgeBases, tool calling, anti-hallucination guardrails). The
  // previous Chat.jsx flow used ``InvokeLLMStream`` which is a raw LLM
  // proxy with no tools — that's why the Industry Research Analyst agent
  // couldn't query the database even though it had ``db_zhanlu_no2`` bound.
  // -------------------------------------------------------------------------
  function compactSelectedSkill(skill) {
    if (!skill || typeof skill !== 'object') return null;
    return {
      id: skill.id || null,
      name: skill.name || '',
      description: skill.description || '',
      trigger: skill.trigger || '',
      category: skill.category || '',
      kind: skill.kind || '',
      source: skill.source || '',
      version: skill.version || '',
      is_default: !!skill.is_default,
    };
  }

  async function handleAgentSend(text, { activeAgent, activeSkill, sid, gen, sessionId, aiMsg, fileUrls, lang, regenerate = false }) {
    // Ask for notification permission once, on the first send (user gesture).
    try {
      if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
        Notification.requestPermission();
      }
    } catch { /* best-effort */ }
    // Lazily create a backend conversation bound to this agent. The
    // v3 endpoint keys off ``conv.agent_name`` and runs the data-source
    // runtime against the matching AgentApp.
    let convId = sessionId;
    if (!convId) {
      // Read the project context from the CURRENT URL params (the
      // source of truth for the current navigation). Previously this
      // read ``sessionStorage['zhanlu:lastProjectContext']``, which
      // leaked: a user who had earlier visited
      // ``/chat?projectName=<project>`` would later open a fresh
      // ``/chat`` (no project param) and find their new conversation
      // auto-tagged with that project — and consequently
      // auto-routed to a project-scoped agent instead of the default
      // ``general_assistant``. Using the URL directly ensures the
      // project context matches the current navigation: only chats
      // opened from a project-scoped URL get tagged with that
      // project; everything else lands in "default / general" with
      // no project_id.
      let projectCtx = {};
      try {
        const url = new URL(window.location.href);
        const urlProjectId = url.searchParams.get('project');
        const urlProjectName = url.searchParams.get('projectName');
        if (urlProjectName) {
          try { projectCtx.projectName = decodeURIComponent(urlProjectName); } catch { projectCtx.projectName = urlProjectName; }
          if (urlProjectId) projectCtx.projectId = urlProjectId;
        } else if (urlProjectId) {
          // URL has project FK but no name — keep the projectId so
          // the backend can still tag the conv. projectName will be
          // resolved server-side from the FK.
          projectCtx.projectId = urlProjectId;
        }
        // If URL has neither, projectCtx stays {} → conv is created
        // with no project_id (the "default / general" path). This is
        // the intended behavior for chats opened outside a project.
      } catch { /* SSR / non-browser env — best-effort */ }
      const conv = await createAgentConversation(activeAgent.name, {
        description: activeAgent.description || '',
        agent_app_id: activeAgent.id,
        // Tag the conversation with the project so the data-source
        // runtime inherits the project's KBs and the conversation
        // shows up in the project's "Recent Chats" list. URL param
        // wins; fall back to the chip selection (pendingProject) the
        // user explicitly picked from the + menu — otherwise a
        // chip-selected project silently chats with the default LLM.
        ...(projectCtx.projectId ? { project_id: projectCtx.projectId } : (pendingProjectId ? { project_id: pendingProjectId } : {})),
        ...(projectCtx.projectName ? { project_name: projectCtx.projectName } : (pendingProject ? { project_name: pendingProject } : {})),
        ...(projectCtx.projectName ? { project: projectCtx.projectName } : (pendingProject ? { project: pendingProject } : {})),
      },
        // Derive the conversation title from the first user message
        // (trimmed, whitespace-normalized, truncated to 60 chars).
        //
        // Previously the metadata object carried the agent's name as
        // a ``name`` field, which the backend (``agents.py`` line
        // 1854-1855) silently treated as the title — so every
        // conversation inherited the agent's name (e.g.
        // "general_assistant") and the Project Detail "Recent Chats"
        // list became unreadable. The top-level ``title`` field is
        // the backend's documented contract for the conversation
        // name; pass it explicitly so the metadata object is free to
        // carry other fields (or nothing).
        //
        // We don't pass a title for empty messages so the backend's
        // default "New Conversation" kicks in (passing "" would store
        // an empty title).
        text ? text.trim().replace(/\s+/g, ' ').slice(0, 60) : undefined);
      convId = conv.id;
      // Link the AgentConversation to the ChatSession so the agent can be
      // auto-restored when this session is reopened from the sidebar.
      //
      // Both columns are added by migration 029 to the ChatSession
      // table (FK to agent_conversations + a plain agent_name string).
      // Before 029 the generic entity service silently dropped both
      // keys because the ChatSession model didn't declare them — the
      // HTTP request returned 200, the frontend assumed the write
      // succeeded, and on the next reload both fields were None.
      //
      // IMPORTANT: we AWAIT this write (it used to be fire-and-forget)
      // so that the next handleSend in the same ChatSession can read
      // ``session.conversation_id`` and reuse the same
      // AgentConversation. The previous fire-and-forget caused a race:
      // the second send happened before the write completed, the
      // resume path saw ``conversation_id = null``, and a brand-new
      // AgentConversation was created — leaving the previous conv
      // orphaned and the Recent Chats list polluted with one row per
      // user message.
      if (sid) {
        try {
          await base44.entities.ChatSession.update(sid, {
            conversation_id: convId,
            agent_name: activeAgent.name,
          });
        } catch { /* best-effort — the link only matters for future reopens */ }
      }
      // Persist the conv id in the URL so the user can:
      //   1. See the chat id in the address bar (the original
      //      "no chat id showing" complaint).
      //   2. Reload the page mid-conversation and resume.
      //   3. Bookmark or share a specific session.
      // We use replaceState (not pushState) so the back button
      // doesn't accumulate one entry per user message.
      try {
        const url = new URL(window.location.href);
        url.searchParams.set('conv', convId);
        // Preserve any other useful params (project, agentName) so
        // a reload re-hydrates the full context.
        if (projectCtx.projectId) url.searchParams.set('project', projectCtx.projectId);
        if (projectCtx.projectName) url.searchParams.set('projectName', projectCtx.projectName);
        // (2026-08-31) Always write agentName too — the user's URL
        // should carry the FULL context (project + agent + conv) so
        // a refresh re-binds the same agent and the LLM keeps its
        // identity. The ?conv= rehydration reads ?agentName= as an
        // override, so this round-trips correctly.
        if (activeAgent && activeAgent.name) url.searchParams.set('agentName', activeAgent.name);
        window.history.replaceState({}, '', url.toString());
      } catch { /* SSR / test env */ }
    }
    // P2: publish the conv id so subsequent Send clicks can route to
    // the steer endpoint while this stream is in flight.
    setStreamingConvId(convId);
    let fullContent = '';
    let toolCalls = [];
    let activitySteps = [];
    let liveEvents = [];      // typed live-activity feed ({type, label_key, params, ts})
    let currentPhase = null;  // latest Claude-style phase headline { state, verb, title }
    let liveTraceSteps = [];  // P0: accumulate trace_step events incrementally
    let reasoningText = '';   // P0: accumulate reasoning from reasoning_done
    let streamingReasoning = ''; // P0-3: per-token reasoning_delta for the live one-liner
    let streamingAction = null;  // P0-3: tool-action text for the live one-liner
    let streamingPlanSteps = []; // 2026-08-27: plan_step_added/completed → live todo checklist
    let finalArtifacts = [];  // artifacts extracted from the done event's conversation
    let sources = [];         // citations extracted from the done event (Kimi/GPT-style source chips)
    let aborted = false;
    try {
      stream.startSending(sid, gen);
      const selectedSkill = compactSelectedSkill(activeSkill);
      // Rename the inner stream variable to `streamGen` — the outer
      // `stream` (from usePersistentStream()) is shadowed by `const stream`
      // here, which creates a Temporal Dead Zone on the line above
      // (`stream.startSending()`). The TDZ throws ReferenceError before
      // the fetch is even made, so the backend never sees the v3 stream
      // POST and the catch block writes "Sorry, the connection was
      // interrupted" to the ChatMessage row.
      const streamGen = streamAgentResponse(
        convId,
        {
          role: 'user',
          content: text,
          ...(regenerate ? { regenerate: true } : {}),
          ...(fileUrls && fileUrls.length ? { file_urls: fileUrls } : {}),
          ...(lang ? { lang } : {}),
          ...(selectedSkill ? { selected_skill: selectedSkill, selected_skill_id: selectedSkill.id } : {}),
          // Forward the current project context in the stream body
          // so the backend's prepare_data_source_runtime can extend
          // the agent's bound KBs with the project's KBs — even when
          // the AgentConversation row was created without a project_id
          // (e.g. legacy convs from before project-scoping landed,
          // or convs first opened outside a project-scoped entry
          // point). Source from the LIVE URL (not pendingProject
          // state, which can lag behind a recent navigation and is
          // cleared by ``newChat(null)`` from "+ New Task"). The
          // backend treats the body project context as a per-message
          // override of ``conv.project_id`` — when the URL has no
          // projectName (a "default / general" chat), no project
          // context is sent, so the agent correctly falls back to
          // its own bound data sources only.
          ...(() => {
            try {
              const url = new URL(window.location.href);
              const urlProjectId = url.searchParams.get('project');
              const urlProjectName = url.searchParams.get('projectName');
              const out = {};
              // URL param wins; fall back to the chip selection
              // (pendingProject) so a project picked from the + menu
              // still routes the message — and its LLM resolution —
              // through that project's configuration.
              if (urlProjectId) out.project_id = urlProjectId;
              else if (pendingProjectId) out.project_id = pendingProjectId;
              if (urlProjectName) {
                try { out.project_name = decodeURIComponent(urlProjectName); } catch { out.project_name = urlProjectName; }
              } else if (pendingProject) {
                out.project_name = pendingProject;
              }
              return out;
            } catch { return {}; }
          })(),
        },
        stream.abortRef.current?.signal,
      );
      for await (const evt of streamGen) {
        if (gen !== genRef.current) {
          // A newer generation has taken over. If a newer stream began,
          // IT owns the shared stream state — resetStream() here would
          // abort the newer stream's controller. Only drain when we are
          // still the last-started stream (a session switch / clear bump
          // orphaned us without replacing the controller).
          if (stream.streamGenRef.current === gen) stream.resetStream();
          return { convId, fullContent, toolCalls };
        }
        if (!evt || typeof evt !== 'object') continue;
        if (evt.type === 'fsm_state' && evt.state) {
          // FSM cognitive-loop state transition (init/goal/plan/act/verify/...).
          // Stored on the message so ActivitySteps can show the planning pipeline.
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, fsm_state: evt.state } : m
            ));
          }
        } else if (evt.type === 'plan_summary' && evt.plan) {
          // Decomposed execution plan from the FSM — nodes + status.
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, plan_summary: evt.plan } : m
            ));
          }
        } else if (evt.type === 'reasoning_delta' && typeof evt.content === 'string') {
          // P0-3 live one-liner: per-token reasoning stream (Kimi/Claude-style).
          // Accumulate raw thinking so the status line shows it char-by-char.
          streamingReasoning += evt.content;
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, streaming_reasoning: streamingReasoning } : m
            ));
          }
        } else if (evt.type === 'plan_step_added' && typeof evt.step_index === 'number') {
          // 2026-08-27: deterministic turn plan — the backend emits the todo
          // list BEFORE the loop starts (plan_step_added) and ticks steps off
          // as tool evidence completes them (plan_step_completed). Accumulate
          // into streaming_plan_steps so LiveActivityStream renders the live
          // checklist ("agent makes a plan, then follows it").
          const existingIdx = streamingPlanSteps.findIndex((s) => s.step_index === evt.step_index);
          const step = { step_index: evt.step_index, title: evt.title || '', status: 'pending' };
          if (existingIdx >= 0) streamingPlanSteps[existingIdx] = { ...streamingPlanSteps[existingIdx], ...step };
          else streamingPlanSteps.push(step);
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, streaming_plan_steps: [...streamingPlanSteps] } : m
            ));
          }
        } else if (evt.type === 'plan_step_completed' && typeof evt.step_index === 'number') {
          const existingIdx = streamingPlanSteps.findIndex((s) => s.step_index === evt.step_index);
          if (existingIdx >= 0) streamingPlanSteps[existingIdx] = { ...streamingPlanSteps[existingIdx], status: 'done' };
          else streamingPlanSteps.push({ step_index: evt.step_index, title: evt.title || '', status: 'done' });
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, streaming_plan_steps: [...streamingPlanSteps] } : m
            ));
          }
        } else if (evt.type === 'tool_progress' && Array.isArray(evt.tool_calls)) {
          toolCalls = evt.tool_calls;
          if (gen === genRef.current) {
            // Derive a short action label for the live one-liner from the
            // most recent tool call (e.g. "running query…").
            const lastCall = evt.tool_calls[evt.tool_calls.length - 1];
            const toolName = lastCall?.name || lastCall?.function?.name || lastCall?.tool_name || '';
            if (toolName) {
              streamingAction = toolName;
              setMessages((prev) => prev.map((m) =>
                m.id === aiMsg.id ? { ...m, streaming_action: toolName } : m
              ));
            }
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, tool_calls: toolCalls } : m
            ));
          }
        } else if (evt.type === 'activity_step' && evt.step && typeof evt.step.number === 'number') {
          // Upsert: update if step number exists, otherwise append
          const incoming = evt.step;
          const existingIdx = activitySteps.findIndex(s => s.number === incoming.number);
          if (existingIdx >= 0) {
            activitySteps[existingIdx] = { ...activitySteps[existingIdx], ...incoming };
          } else {
            activitySteps.push(incoming);
          }
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, activity_steps: [...activitySteps] } : m
            ));
          }
        } else if (evt.type === 'live_event' && evt.event) {
          // Typed live-activity feed: accumulate {type, label_key, params, ts}
          // containers so LiveActivityStream renders the inline stream.
          liveEvents.push(evt.event);
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, live_events: [...liveEvents] } : m
            ));
          }
        } else if (evt.type === 'phase' && evt.verb) {
          // Claude-style phase headline (Fathoming / Fabricating / …).
          // Keep the latest one on the message; ActivitySteps renders it
          // above the step list.
          currentPhase = { state: evt.state, verb: evt.verb, title: evt.title };
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, phase: currentPhase } : m
            ));
          }
        } else if (evt.type === 'artifact_created' && evt.artifact) {
          // Live artifact (Claude-style side panel): the moment an artifact
          // exists mid-turn, attach it to the message. The preview pane is
          // only opened when the user clicks "Open" on the artifact card.
          const art = evt.artifact;
          const artId = art.artifact_id || art.id;
          if (artId && !finalArtifacts.some((a) => (a.artifact_id || a.id) === artId)) {
            finalArtifacts = [...finalArtifacts, art];
          }
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, artifacts: [...finalArtifacts] } : m
            ));
          }
        } else if (evt.type === 'trace_step' && evt.step) {
          // P0: incremental trace_step from the v3 SSE. Append to the
          // live trace (consumed by activity steps / reasoning panel).
          liveTraceSteps.push(evt.step);
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, trace_steps: [...liveTraceSteps] } : m
            ));
          }
        } else if (evt.type === 'reasoning_done' && typeof evt.reasoning === 'string') {
          // P0: reasoning_done captures the full reasoning text. We accept
          // any subsequent reasoning_done as a final replace (multiple
          // iterations each emit one — we keep the last non-empty one).
          if (evt.reasoning) {
            reasoningText = evt.reasoning;
            if (gen === genRef.current) {
              setMessages((prev) => prev.map((m) =>
                m.id === aiMsg.id ? { ...m, reasoning: reasoningText } : m
              ));
            }
          }
        } else if (evt.type === 'delta' && typeof evt.content === 'string') {
          fullContent += evt.content;
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id
                ? { ...m, content: fullContent, refining: false }
                : m
            ));
            // Persist partial content so it survives page navigation
            stream.updatePendingMessage(sid, { id: aiMsg.id, content: fullContent, loading: true });
          }
        } else if (evt.type === 'content_preserve' && typeof evt.content === 'string') {
          // 2026-08-25: the server signals that the current bubble's text
          // should be PRESERVED (kept visible) while the next iteration's
          // stream replaces it. This eliminates the "collapse" UX where
          // the user's visible text suddenly disappears. We set a
          // `refining: true` flag so the bubble can show a subtle
          // "Refining answer..." indicator. The subsequent content_replace
          // (or streamed deltas) will clear this flag.
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id
                ? { ...m, refining: true, refiningReason: evt.reason || 'nudge' }
                : m
            ));
          }
        } else if (evt.type === 'content_replace' && typeof evt.content === 'string') {
          // D2 (2026-08-20): post-nudge re-synthesis. The server replaces the
          // whole bubble (REPLACE, not append) so leaked nudge-reply prose
          // never shows. Each event carries the full accumulated text; the
          // `done` event still carries the authoritative final content.
          // 2026-08-25: clear the `refining` flag set by content_preserve.
          fullContent = evt.content;
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id
                ? { ...m, content: fullContent, refining: false }
                : m
            ));
            // Persist partial content so it survives page navigation
            stream.updatePendingMessage(sid, { id: aiMsg.id, content: fullContent, loading: true });
          }
        } else if (evt.type === 'error') {
          // Bug 1 fix: don't surface raw server error text in the chat.
          // Show a clean, localized message instead. The raw error is
          // still available in the browser console for debugging.
          const friendlyErr = lang === 'en'
            ? "Sorry, I hit an error while responding. Please try again."
            : "抱歉，回复时出错了，请重试。";
          console.error('[Chat stream error]', evt.message || evt);
          fullContent = fullContent || friendlyErr;
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id ? { ...m, content: fullContent } : m
            ));
          }
        } else if (evt.type === 'done') {
          // Defensive merge (2026-08-20): the `done` event's content is the
          // server-authoritative message, but in multi-iteration turns it can
          // carry only the LAST iteration's prose. Prefer the LONGER of
          // (locally-streamed delta accumulation, server content) so earlier
          // iterations' text (tables, findings, recommendations) is never
          // wiped from the visible bubble.
          if (typeof evt.content === 'string' && evt.content.length > (fullContent?.length || 0)) {
            fullContent = evt.content;
          }
          // The done event carries the authoritative assistant message
          // (with full tool_calls + artifacts) inside evt.conversation.
          // Extract them so charts (report_card_payload in tool_calls) and
          // file artifact cards survive a page refresh — previously these
          // were only held in memory and lost on reload because the
          // chat_messages row was never updated with them.
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
          // Kimi/GPT-style citations: read the sources the backend attached
          // to the assistant message (FSM path also mirrors them in
          // evt.sources). Persisted so source chips survive a refresh.
          // NOTE: assigns to the outer accumulator (hoisted above the
          // loop) so the final ChatMessage.update below can persist them.
          if (Array.isArray(lastAssistant?.sources) && lastAssistant.sources.length) {
            sources = lastAssistant.sources;
          } else if (Array.isArray(evt.sources) && evt.sources.length) {
            sources = evt.sources;
          }
          if (gen === genRef.current) {
            setMessages((prev) => prev.map((m) =>
              m.id === aiMsg.id
                ? {
                  ...m,
                  content: fullContent,
                  tool_calls: toolCalls,
                  refining: false,
                  ...(finalArtifacts.length ? { artifacts: finalArtifacts } : {}),
                  ...(sources.length ? { sources } : {}),
                }
                : m
            ));
            // Persist final content so it survives page navigation
            stream.updatePendingMessage(sid, { id: aiMsg.id, content: fullContent, toolCalls, loading: false });
            // P2 (conversational dashboards): scan the freshly-pushed
            // tool_calls for a completed dashboard tool. Completed live
            // dashboards are surfaced as first-class artifacts, sharing the
            // same right-side preview pane as files/reports instead of the
            // old dashboard-only dock panel.
            const seen = dashboardSeenRef.current.get(aiMsg.id) || 0;
            const nextSeen = watchForDashboardEdits(toolCalls, seen, {
              onArtifact: (artifact) => {
                const artId = artifact.artifact_id || artifact.id;
                if (artId && !finalArtifacts.some((a) => (a.artifact_id || a.id) === artId)) {
                  finalArtifacts = [...finalArtifacts, artifact];
                }
                setMessages((prev) => prev.map((m) => (
                  m.id === aiMsg.id ? { ...m, artifacts: [...finalArtifacts] } : m
                )));
              },
              onRefresh: () => { },
            });
            if (nextSeen !== seen) dashboardSeenRef.current.set(aiMsg.id, nextSeen);
          }
          // FIX: the agent flow used to leave the stream state stuck in
          // 'streaming' after a normal `done` event, so the Stop button
          // stayed visible. Drain the state here so the input flips back
          // to Send.
          if (gen === genRef.current) stream.complete();
          // Automation-bound chat: dismiss the editable draft card when
          // the agent successfully created the automation task via the
          // ``create_automation`` tool. The draft card is a pre-creation
          // editable summary; once the task exists server-side, it has
          // served its purpose. Also dispatch ``zhanlu:automation-created``
          // so other mount points (SessionList clock badge) refresh.
          if (gen === genRef.current && Array.isArray(toolCalls) && toolCalls.some((tc) => {
            const name = tc.name || (tc.function && tc.function.name) || '';
            return name === 'create_automation';
          })) {
            setPendingDraft(null);
            try {
              window.dispatchEvent(new CustomEvent('zhanlu:automation-created', {
                detail: { session_id: sid },
              }));
            } catch { /* non-fatal */ }
          }
        }
      }
      // Defensive: if the stream ended without emitting `done` (e.g. the
      // server closed the connection), still drain the state.
      if (gen === genRef.current && stream.isActive) stream.complete();
    } catch (streamErr) {
      // AbortError → user clicked Stop. Append a hint and stop.
      if (streamErr && (streamErr.name === 'AbortError' || streamErr.code === 20)) {
        aborted = true;
        if (gen === genRef.current) {
          const stoppedText = lang === 'en' ? '\n\n[Stopped generating.]' : '\n\n[已停止生成。]';
          setMessages((prev) => prev.map((m) =>
            m.id === aiMsg.id ? { ...m, content: (m.content || '') + stoppedText } : m
          ));
        }
        // Clear pending buffer on abort so stale partial content isn't injected on remount
        stream.clearPendingMessage(sid);
        return { convId, fullContent, toolCalls, aborted: true };
      }
      if (gen === genRef.current) {
        // SessionExpiredError → the user's tokens are dead (refresh token
        // revoked/replayed, mid-session expiry, etc). Show an actionable,
        // localized message instead of the generic "connection interrupted"
        // one — and CRITICALLY, do NOT persist the error text to the
        // ChatMessage row (via the later ChatMessage.update). The user
        // already sees error messages persisted by the legacy /auth/refresh
        // bug; this branch stops creating new ones. Trigger a forced logout
        // so AuthContext + redirect land the user back on /login.
        if (streamErr && streamErr.sessionExpired) {
          console.warn('[Chat] session expired mid-stream — forcing re-login:', streamErr);
          const expiredText = lang === 'en'
            ? '⚠️ Your session has expired. Please log in again to continue.'
            : '⚠️ 登录已过期，请重新登录后再继续。';
          setMessages((prev) => prev.map((m) =>
            m.id === aiMsg.id ? { ...m, content: expiredText } : m
          ));
          stream.clearPendingMessage(sid);
          stream.complete();
          // Trigger re-auth without leaving the user stuck. AuthContext.logout
          // already does the localStorage clear + redirect, so the next
          // page load lands them on the login screen with full state cleared.
          try {
            await base44.auth.logout?.();
          } catch { /* best-effort */ }
          // Direct local cleanup in case the SDK's logout is async or broken.
          try {
            localStorage.removeItem('base44_access_token');
            localStorage.removeItem('token');
            localStorage.removeItem('refresh_token');
          } catch { /* ignore */ }
          // Defer the redirect a tick so React flushes the message update.
          setTimeout(() => { window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search); }, 100);
          return { convId, fullContent: expiredText, toolCalls, sessionExpired: true };
        }
        // Bug 1 fix: don't surface raw network/stream errors in the chat
        // content. Show a clean, localized message. The raw error is
        // still available in the browser console for debugging. This
        // also stops the raw error from being persisted to the DB via
        // the ChatMessage.update call below.
        console.error('[Chat stream error]', streamErr);
        const friendlyErr = lang === 'en'
          ? "Sorry, the connection was interrupted. Please try again."
          : "抱歉，连接中断了，请重试。";
        fullContent = fullContent || friendlyErr;
        setMessages((prev) => prev.map((m) =>
          m.id === aiMsg.id ? { ...m, content: fullContent } : m
        ));
        // Clear pending buffer on error so stale partial content isn't injected on remount
        stream.clearPendingMessage(sid);
        // Only drain the shared stream state if this generation is still
        // current — a stale stream must not clear a newer session's
        // active stream state.
        if (gen === genRef.current) stream.complete();
      }
    }
    // Persist the final assistant message + tool_calls + artifacts +
    // activity_steps so charts and file cards survive a page refresh.
    try {
      await base44.entities.ChatMessage.update(aiMsg.id, {
        content: fullContent,
        ...(toolCalls.length ? { tool_calls: toolCalls } : {}),
        ...(activitySteps.length ? { activity_steps: activitySteps } : {}),
        ...(liveEvents.length ? { live_events: liveEvents } : {}),
        ...(finalArtifacts.length ? { artifacts: finalArtifacts } : {}),
        ...(currentPhase ? { phase: currentPhase } : {}),
        ...(sources.length ? { sources } : {}),
      });
      await base44.entities.ChatSession.update(sid, { last_message_at: new Date().toISOString() });
      touchSession(sid);
    } catch { /* best-effort persist */ }
    // P2: clear the streaming conv id now that the in-flight stream is
    // done — the next Send click should start a new stream, not steer.
    setStreamingConvId((cur) => (cur === convId ? null : cur));
    return { convId, fullContent, toolCalls, aborted };
  }

  // Kimi/GPT-style regenerate: re-run the LAST user turn against the same
  // conversation. The backend (``regenerate: true``) reuses the existing
  // user message (no duplicate user bubble), re-runs the agent loop, and
  // REPLACES the previous assistant message — so the UI simply streams the
  // new answer into the SAME assistant bubble (same id, no orphan rows).
  // Only available on the LAST assistant message while idle.
  async function handleRegenerate(targetMsg) {
    if (!targetMsg || targetMsg.role !== 'assistant' || streamingId || loading) return;
    const idx = messages.findIndex((m) => m && m.id === targetMsg.id);
    if (idx < 0) return;
    // Locate the user prompt that produced this assistant reply (walk
    // back past any hidden/system markers to the nearest user message).
    let userIdx = idx - 1;
    while (userIdx >= 0 && messages[userIdx]?.role !== 'user') userIdx--;
    if (userIdx < 0) return;
    const userMsg = messages[userIdx];
    if (!userMsg || !userMsg.content) return;
    const gen = ++genRef.current;
    const sid = activeId;
    if (!sid) return;
    // Reuse the SAME assistant message object (id preserved) so the
    // stream's done handler merges into the existing bubble and the
    // final ChatMessage.update rewrites the same row — no duplicates.
    const aiMsg = { ...targetMsg, content: '', tool_calls: [], artifacts: [], sources: [], refining: true };
    setMessages((prev) => prev.map((m) => (m && m.id === targetMsg.id ? aiMsg : m)));
    setStreamingId(aiMsg.id);
    setLoading(true);
    let effectiveAgent = activeAgent;
    if (!effectiveAgent) {
      try {
        const _agents = await base44.entities.AgentApp.list();
        effectiveAgent = _agents.find((a) => a.name === 'general_assistant') || null;
        if (!effectiveAgent) effectiveAgent = { name: 'general_assistant' };
      } catch {
        effectiveAgent = { name: 'general_assistant' };
      }
    }
    try {
      const effectiveAiLang = aiLang === 'auto' ? (detectLang(userMsg.content) || 'zh') : aiLang;
      // Resolve the conv id to reuse (same priority as handleSend).
      let resumeConvId = streamingConvId;
      if (!resumeConvId && sid) {
        const sess = getSession ? getSession(sid) : null;
        resumeConvId = sess?.conversation_id || null;
      }
      if (!resumeConvId) {
        try {
          const urlConv = new URLSearchParams(window.location.search).get('conv');
          if (urlConv) resumeConvId = urlConv;
        } catch { /* SSR / test env */ }
      }
      await handleAgentSend(userMsg.content, {
        activeAgent: effectiveAgent,
        activeSkill: activeSkill,
        sid,
        gen,
        sessionId: resumeConvId,
        aiMsg,
        fileUrls: [],  // backend re-reads the original turn's file_urls from conv.messages
        lang: effectiveAiLang,
        regenerate: true,
      });
    } finally {
      if (gen === genRef.current) {
        setLoading(false);
        setStreamingId(null);
        if (stream.isActive) stream.complete();
      }
    }
  }

  async function handleClear() {
    genRef.current++;
    setStreamingId(null);
    setActiveSkill(null);
    setActiveAgent(null);
    setChips([]);
    setInputValue('');
    clearDraft('chat_input_draft');
    // Also abort any in-flight stream so the Stop button doesn't linger.
    // Only when THIS session owns the active stream — another session's
    // in-flight stream must keep running untouched.
    if (stream.isActiveForSession(activeId)) stream.resetStream();
    stream.clearPendingMessage(activeId);
    if (activeId) {
      await base44.entities.ChatMessage.deleteMany({ session_id: activeId });
      setMessages([]);
    }
  }

  async function handleDelete(s) {
    // Delegates to ChatSessionContext (which already removes the
    // session from the sidebar list and deletes the backend row).
    // We only need to handle the chat-page-local side effects: bump
    // the generation counter so any in-flight stream is invalidated,
    // and clear the messages if the deleted session is currently
    // active.
    genRef.current++;
    setStreamingId(null);
    stream.clearPendingMessage(s.id);
    if (activeId === s.id) {
      setMessages([]);
      // Context clears activeId; the active session is gone.
    }
    await deleteSession(s);
  }

  // Stop button handler for automation runs. The "response" the user
  // is watching in the chat surface is actually a background executor
  // spawned by ``runAutomationTask`` — there is no SSE stream to abort
  // here, so the Stop button calls the dedicated cancel API. The
  // existing by-session poll will pick up the new status (cancelled)
  // on its next tick and clear the streaming UI; we don't have to do
  // that ourselves.
  async function handleStopAutomation() {
    const exe = latestExecution;
    if (!exe || !exe.id) {
      return;
    }
    if (exe.status !== 'queued' && exe.status !== 'running') {
      return;
    }
    const r = await cancelAutomationExecution(exe.id, { authFetch });
    if (!r.ok) {
      // Soft failure: log but don't throw — the user can click Stop
      // again, and the next by-session tick will still surface the
      // terminal state if the call DID land.
      console.warn('[Chat] automation cancel failed:', r.error);
    }
  }

  // Combined busy flag for the chat input: true while either the SSE
  // chat stream is active FOR THIS SESSION OR an automation run is in
  // flight. Scoped per session so navigating to a new chat while another
  // session is still streaming leaves the new session's input enabled
  // (concurrent chat support). Used to swap Send → Stop, disable typing,
  // and route the Stop click.
  const chatInputBusy = stream.isActiveForSession(activeId) || isRunInFlight(latestExecution);

  // For an in-flight automation run, the executor pre-creates an empty
  // assistant bubble (phase.execution_id = latestExecution.id) which
  // ``MessageBubble`` then populates with real activity steps. As soon
  // as that bubble exists in ``messages``, hide the 3 generic
  // placeholders so the user sees the real "✨ Fabricating" /
  // "Crystallizing" ActivitySteps component — no visual double-up.
  // When the bubble is missing (the very first ~100ms after Run Now
  // before the marker lands), keep the generic placeholders so the
  // user has visual confirmation the run actually started.
  const inFlightExecutionId = (latestExecution && isRunInFlight(latestExecution))
    ? latestExecution.id
    : null;
  const hasAssistantBubbleForRun = inFlightExecutionId
    ? messages.some((m) => {
        if (!m || m.role !== 'assistant') return false;
        const phase = m.phase && typeof m.phase === 'object' ? m.phase : null;
        return phase && phase.execution_id === inFlightExecutionId;
      })
    : false;

  function handleSelectSession(id) {
    // Route selection through the context so the SessionList in the
    // sidebar highlights the new active row. Previously this lived
    // in the SessionList's onSelect callback; now SessionList just
    // calls `selectSession` from the context.
    genRef.current++;
    setStreamingId(null);
    stream.clearPendingMessage(activeId);  // clear buffer for the old session
    setActiveId(id);
    // Adopt the session's project so the next new chat inherits it
    // (consistent with the previous behavior).
    adoptSessionProject(id);
    setOpenArtifact(null);
  }

  function handleNewChat(project) {
    // "+ New Task" or "+" on a project group. The actual session
    // creation happens lazily on first send; here we just reset the
    // in-flight state and stage the project.
    genRef.current++;
    setStreamingId(null);
    setStreamingConvId(null);  // clear the resume handle so the next send creates a new conv
    stream.clearPendingMessage(activeId);  // clear buffer for the old session
    setMessages([]);
    setChips([]);
    setActiveSkill(null);
    setActiveAgent(null);
    setAttachments([]);
    setOpenArtifact(null);
    contextNewChat(project || pendingProject);
    // Clear the ?conv= URL param so a reload / share of this
    // address doesn't accidentally resume the previous
    // conversation. The user explicitly asked for the chat id
    // to be visible — but the moment they click "+ New Task",
    // the previous id no longer applies.
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete('conv');
      // Keep project / projectName so the new chat starts in the
      // same project group.
      if (project) {
        // Pending project — let the user pick the project first;
        // we don't have its id at this point.
      }
      window.history.replaceState({}, '', url.toString());
    } catch { /* SSR / test env */ }
  }

  // (handleStar and handleRename were removed — the context's
  // `starSession` and `renameSession` already handle the optimistic
  // UI update + backend persistence.)

  function handleAddChip(key) {
    setChips((prev) => (prev.includes(key) ? prev : [...prev, key]));
  }
  function handleRemoveChip(key) {
    setChips((prev) => prev.filter((k) => k !== key));
  }
  function handleSelectItem(item) {
    setActiveSkill(item.skillObj);
    setInputValue(item.text);
  }
  function handlePickSkill(skillObj) { setActiveSkill(skillObj); }
  function handlePickAgent(agent) { setActiveAgent(agent); }
  function handleRemoveSkill() {
    setActiveSkill(null);
  }
  function handleRemoveAgent() {
    setActiveAgent(null);
  }
  function handleSelectOption(label) {
    // When the option label starts with a system-prompt marker,
    // treat it as a hidden synthetic message (auto-collect flow).
    if (label.startsWith('【系统提示】')) {
      handleSend(label, { hidden: true });
      return;
    }
    // Automation-bound chats: send the option selection as a hidden
    // system hint so the automation_agent binds it to the task (not as
    // a report request). The agent's ANSWER-BINDING RULE (in its system
    // prompt) then turns the named source into a
    // create_automation/update_automation call with data_source_id set.
    //
    // We detect automation-bound chats via three signals:
    //   1. The latest assistant message contains a [[CLARIFY]] block
    //      (the automation_agent's disambiguation format).
    //   2. activeAgent is already automation_agent (conv rehydration).
    //   3. pendingDraft is set (Automation dialog origin).
    const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
    const isAutomationClarify = !!(lastAssistant && (lastAssistant.content || '').includes('[[CLARIFY]]'));
    if (isAutomationClarify || (activeAgent && activeAgent.name === 'automation_agent') || pendingDraft) {
      const hint = `【系统提示】用户选择了：${label}。请将其作为配置答案处理——如果是数据源，调用 create_automation 或 update_automation 绑定 data_source_id（用 list_knowledge_bases 查到的匹配 id），然后继续设置或确认创建。绝不要将其当作报告或数据分析请求。`;
      handleSend(hint, { hidden: true });
    } else {
      handleSend(label);
    }
  }
  async function handleSaved(updated) {
    setPreviewResult(updated);
    setMessages((prev) => prev.map((m) => {
      if (m.role !== 'assistant' || !(m.content || '').includes('[[RESULT]]')) return m;
      const regex = /(\[\[RESULT\]\]\s*\n?)([\s\S]*?)(\[\[END\]\])/g;
      const newContent = m.content.replace(regex, (full, pre, json, post) => {
        try {
          const parsed = JSON.parse(json.trim());
          if (parsed.draft && parsed.name === updated.name && parsed.type === updated.type) {
            return `${pre}${JSON.stringify({ type: updated.type, id: updated.id, name: updated.name })}${post}`;
          }
        } catch { /* leave block as-is */ }
        return full;
      });
      if (newContent !== m.content) {
        base44.entities.ChatMessage.update(m.id, { content: newContent });
        return { ...m, content: newContent };
      }
      return m;
    }));
  }
  function handleSelectOther() {
    inputRef.current?.focus();
  }

  // Batch clarify submit: send the assembled answers as a single user message.
  function handleBatchClarifySubmit(answerText) {
    handleSend(answerText);
  }

  // Edit & resend: pre-fill the input with the original message text
  // so the user can tweak and resend. This enables message branching
  // without needing a full fork-history UI.
  function handleEditMessage(msg) {
    setInputValue(msg.content || '');
    inputRef.current?.focus();
  }

  // Preview a created agent: navigate to its config page.
  function handleAgentPreview(agentId) {
    if (agentId) navigate(`/my-space/agent/${agentId}`);
  }

  // Run a created agent: load it into the current chat session.
  async function handleAgentRun(agentId) {
    if (!agentId) return;
    try {
      const agent = await base44.entities.AgentApp.get(agentId);
      setActiveAgent(agent);
      setInputValue('');
      // No explicit clearDraft here: the useEffect on
      // [inputValue] mirrors the cleared value to localStorage
      // on the next tick, so the draft is empty on refresh.
      // Bound Kbs are no longer surfaced in the chat input (no more
      // "DB: …" chip / "Read from my database" button). The binding
      // itself is still on ``agent.knowledge_bases`` and is consumed
      // by the backend data-source runtime at execution time.
    } catch { /* noop */ }
  }

  // Manus-style side panel was removed in the Claude-style redesign; the
  // artifact preview now opens as a right-anchored pane (see ArtifactPreviewPane).

  async function ensureSession(firstText) {
    if (activeId) return activeId;
    const session = await base44.entities.ChatSession.create({
      title: (firstText || t.chat.coreTitle).slice(0, 24),
      // See the parallel comment in handleSend: do NOT store the
      // Ungrouped i18n string. Store null so the chat input chip
      // can be hidden cleanly (no "Ungrouped" chip for the
      // implicit default) and so project-scoped queries can
      // filter on ``project_id IS NULL`` consistently.
      project: pendingProject || null,
      // Set the project FK too when the user came in via a project
      // page — keeps the session visible in that project's
      // "Recent Chats" tab and lets project-scoped queries find it.
      ...(pendingProjectId ? { project_id: pendingProjectId } : {}),
      last_message_at: new Date().toISOString(),
    });
    setActiveId(session.id);
    prependSession(session);
    return session.id;
  }

  async function handleUploadFile(file) {
    let file_url;
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    const kind = ['csv', 'xls', 'xlsx', 'json', 'tsv'].includes(ext) ? 'data_file'
      : ['html', 'htm'].includes(ext) ? 'html_file' : 'document';

    // 0) Ensure a session exists BEFORE the chip renders. Creating a
    // session calls setActiveId(), and the session-change effect clears
    // draft attachments (setAttachments([])) — if the chip is rendered
    // first, the effect wipes it a tick later and the user sees nothing
    // (the "file uploaded but never shown, agent never gets it" bug on
    // the landing page, where activeId is null). Running ensureSession
    // here makes the wipe-effect fire while attachments is still empty;
    // the chip lands after, with no subsequent activeId change to clear
    // it. Best-effort: if session creation fails we still upload and
    // render the chip (persistence just won't have a session link).
    let sid = null;
    try {
      sid = await ensureSession(file.name);
    } catch (err) {
      console.error('[upload] ensureSession failed (chip still rendered):', err);
    }

    // 1) Upload the bytes first so the chip can render even if persistence fails.
    try {
      const uploaded = await base44.integrations.Core.UploadFile({ file });
      file_url = uploaded?.file_url;
      if (!file_url) throw new Error('Upload returned no file_url');
    } catch (err) {
      console.error('[upload] UploadFile failed:', err);
      toast.error(`Upload failed: ${file.name}`);
      return; // chip never appears; the picker stays open
    }

    // 2) Render the chip immediately so the user sees their file.
    setAttachments((prev) => [...prev, { name: file.name, file_url, ext, kind }]);

    // 3) Best-effort persistence — don't block the chip if this fails.
    try {
      if (sid) {
        await base44.entities.UserFile.create({
          name: file.name, file_type: ext, size: file.size, file_url, source: 'upload',
          resource_kind: kind, session_id: sid, project: pendingProject || t.sessionList.ungrouped,
        });
      }
    } catch (err) {
      console.error('[upload] persistence failed (chip still rendered):', err);
    }
  }

  return (
    <div className="flex h-full">
      <PanelGroup direction="horizontal" className="flex-1 min-w-0">
        <Panel id="chat-panel" order={1} defaultSize={panels.chat.defaultSize} minSize={panels.chat.minSize}>
          <div className="flex h-full flex-col">
            {activeId || messages.length > 0 ? (
              <>
                <div className="flex items-center justify-between border-b border-border px-6 py-2">
                  {/* Project context banner — shown when the user entered
                  chat from a project page (via the "chat with agent"
                  shortcut). Clicking the breadcrumb returns to the
                  project. Hidden when the chat is not project-scoped. */}
                  {pendingProject ? (
                    <button
                      type="button"
                      onClick={() => { if (pendingProjectId) navigate(`/my-space/project/${pendingProjectId}`); }}
                      className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                      title={isEn ? 'Back to project' : '返回项目'}
                    >
                      <FolderKanban className="h-3.5 w-3.5 shrink-0 text-primary" />
                      <span className="min-w-0 flex-1 truncate font-medium text-foreground">{pendingProject}</span>
                      <ArrowLeft className="h-3 w-3 shrink-0" />
                    </button>
                  ) : <span />}
                  {dashboardMode ? (
                    <button
                      type="button"
                      onClick={() => navigate(`/dashboard/${dashboardMode.slug}`)}
                      className="inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary transition-colors hover:bg-primary/15"
                      title={isEn ? 'This session is dedicated to building/editing this dashboard — click to open it' : '此会话专用于构建/编辑该仪表盘 — 点击打开'}
                    >
                      <LayoutDashboard className="h-3.5 w-3.5" />
                      <span className="max-w-[220px] truncate">{dashboardMode.name}</span>
                      <ArrowLeft className="h-3 w-3 rotate-180" />
                    </button>
                  ) : null}
                  <div className="flex items-center gap-1.5">
                    {/* Manus-style Scheduled button — only rendered when the
                    current session is the origin of an automation. */}
                    {sessionAutomation && !isAutomationSession && (
                      <button
                        onClick={() => setScheduledOpen(true)}
                        className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/10"
                        title={isEn ? 'Scheduled automation' : '定时任务'}
                      >
                        <Clock className="h-3.5 w-3.5" />
                        {isEn ? 'Scheduled' : '定时任务'}
                      </button>
                    )}
                    <button onClick={() => setFilesOpen(true)} className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground">
                      <FolderOpen className="h-3.5 w-3.5" /> {t.chat.chatFiles.title}
                    </button>
                  </div>
                </div>
                <div ref={scrollRef} className="relative flex-1 space-y-6 overflow-y-auto px-6 py-6">
                  <div className="mx-auto max-w-5xl space-y-6">
                    {loadStatus === 'loading' && messages.length === 0 ? (
                      <div className="animate-pulse space-y-6 py-8">
                        {[1, 2, 3].map((i) => (
                          <div key={i} className={`flex gap-3 ${i % 2 === 0 ? 'flex-row-reverse' : ''}`}>
                            <div className="h-8 w-8 shrink-0 rounded-full bg-secondary" />
                            <div className={`flex flex-col gap-2 ${i % 2 === 0 ? 'items-end' : 'items-start'}`}>
                              <div className="h-14 rounded-2xl bg-secondary" style={{ width: `${180 + i * 70}px` }} />
                              <div className="h-3 rounded-full bg-secondary" style={{ width: `${100 + i * 30}px` }} />
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="space-y-6">
                        {pendingDraft && (
                          <AutomationDraftCard
                            prefill={pendingDraft.prefill}
                            lang={pendingDraft.lang}
                            error={pendingDraft.error}
                            onAppendToInput={(text) => {
                              setInputValue((cur) => (cur ? `${cur}\n${text}` : text));
                              // Focus the input so the user can review and send
                              requestAnimationFrame(() => {
                                try { inputRef.current?.focus(); } catch { }
                              });
                            }}
                            onDismiss={() => setPendingDraft(null)}
                            onRetry={() => {
                              // Clear the error and re-send the prefill as a
                              // fresh user message so the LLM gets another
                              // chance to create the task.
                              setPendingDraft((d) => d ? { ...d, error: null } : d);
                              const fresh = pendingDraft.prefill;
                              if (fresh) {
                                setInputValue(fresh);
                                requestAnimationFrame(() => {
                                  try { inputRef.current?.focus(); } catch { }
                                });
                              }
                            }}
                          />
                        )}
                        {(() => {
                          // Visible = non-hidden + (streaming bubble OR any
                          // assistant message that has content). The
                          // Regenerate action (Kimi/GPT-style) is offered
                          // ONLY on the LAST visible assistant message while
                          // idle — regenerating mid-list would rewrite a
                          // message whose children (follow-ups) depend on it.
                          const visible = messages.filter((m) => !m.hidden && (m.id === streamingId || !(m.role === 'assistant' && !m.content)));
                          const lastId = visible.length ? visible[visible.length - 1].id : null;
                          const idle = !streamingId && !loading;
                          return visible.map((m) => {
                            const isStreaming = m.id === streamingId;
                            const hasMarkers = (m.content || '').includes('[[CLARIFY]]') || (m.content || '').includes('[[CLARIFY_BATCH]]') || (m.content || '').includes('[[RESULT]]');
                            const canRegenerate = m.role === 'assistant' && !!m.content && m.id === lastId && idle && !isStreaming;
                            return (
                              <MessageBubble key={m.id} message={{ ...m, content: m.content || '' }} isStreaming={isStreaming} onSelectOption={handleSelectOption} onSelectOther={handleSelectOther} onPreview={setPreviewResult} onArtifactPreview={setOpenArtifact} onArtifactEdit={setCanvasArtifact} onPreviewFile={setOpenFilePreview} onBatchClarifySubmit={handleBatchClarifySubmit} onAgentPreview={handleAgentPreview} onAgentRun={handleAgentRun} onOpenRun={isAutomationSession ? undefined : openRun} isAutomationSession={isAutomationSession} userRequestedFormat={userFormatByMessage[m.id] || null} onEditMessage={handleEditMessage} onFeedback={m.role === 'assistant' ? handleMessageFeedback : null} feedbackRating={feedbackByMessage[m.id] || null} showRoleRating={m.role === 'assistant' ? roleRatingMessageIds.has(m.id) : false} onRoleRelevance={m.role === 'assistant' ? handleRoleRelevance : null} roleRelevanceRating={roleRelevanceByMessage[m.id] || null} onRegenerate={canRegenerate ? handleRegenerate : null} />
                            );
                          });
                        })()}
                      </div>
                    )}
                    {/* 2026-08-31: removed ChatThinkingIndicator and steerMarkers.
                    The assistant bubble now renders LiveActivityStream from the
                    very first frame (rich placeholder while no events have
                    arrived), so the legacy "Understanding your request" phase
                    list and the "→ steer" pills are no longer needed. */}
                  </div>
                </div>
                <ChatInput value={inputValue} onChange={setInputValue} onSend={handleSend} onClear={handleClear} chips={chips} categoryLabels={CATEGORIES.reduce((acc, c) => { acc[c.key] = t.chat.categories[c.key].label; return acc; }, {})} onRemoveChip={handleRemoveChip} activeSkill={activeSkill} onRemoveSkill={handleRemoveSkill} activeAgent={activeAgent} onRemoveAgent={handleRemoveAgent} onSelectSkill={handlePickSkill} onSelectAgent={handlePickAgent} disabled={loading || isRunInFlight(latestExecution)} isStreaming={chatInputBusy} onStop={stream.abort} onStopAutomation={isRunInFlight(latestExecution) ? handleStopAutomation : undefined} inputRef={inputRef} onUploadFile={handleUploadFile} attachments={attachments} onRemoveAttachment={(i) => setAttachments((prev) => prev.filter((_, idx) => idx !== i))} pendingProject={pendingProject} onClearProject={() => setPendingProject(null)} onSelectProject={setPendingProject} inheritedKbCount={inheritedKbCount} pendingProjectId={pendingProjectId} />
              </>
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center px-6 pb-12">
                <div className="mb-5 flex h-12 w-12 items-center justify-center rounded-full bg-secondary">
                  <Bot className="h-6 w-6 text-primary" />
                </div>
                <h1 className="break-words px-4 text-center font-display text-3xl tracking-tight text-foreground">{t.chat.coreTitle}</h1>
                <div className="mt-8 w-full max-w-4xl">
                  <ChatInput value={inputValue} onChange={setInputValue} onSend={handleSend} onClear={handleClear} chips={chips} categoryLabels={CATEGORIES.reduce((acc, c) => { acc[c.key] = t.chat.categories[c.key].label; return acc; }, {})} onRemoveChip={handleRemoveChip} activeSkill={activeSkill} onRemoveSkill={handleRemoveSkill} activeAgent={activeAgent} onRemoveAgent={handleRemoveAgent} onSelectSkill={handlePickSkill} onSelectAgent={handlePickAgent} disabled={loading || isRunInFlight(latestExecution)} isStreaming={chatInputBusy} onStop={stream.abort} onStopAutomation={isRunInFlight(latestExecution) ? handleStopAutomation : undefined} inputRef={inputRef} onUploadFile={handleUploadFile} attachments={attachments} onRemoveAttachment={(i) => setAttachments((prev) => prev.filter((_, idx) => idx !== i))} pendingProject={pendingProject} onClearProject={() => setPendingProject(null, null)} onSelectProject={(name, id) => setPendingProject(name, id)} centered inheritedKbCount={inheritedKbCount} pendingProjectId={pendingProjectId} />
                  {chips.length > 0 && (
                    <RelatedContent
                      chips={chips}
                      related={t.chat.related}
                      categoryLabels={CATEGORIES.reduce((acc, c) => { acc[c.key] = t.chat.categories[c.key].label; return acc; }, {})}
                      onRemoveChip={handleRemoveChip}
                      onSelectItem={handleSelectItem}
                    />
                  )}
                </div>
              </div>
            )}
          </div>
        </Panel>
        {scheduledOpen && (
          <>
            <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50 data-[resize-handle-state=drag]:bg-primary" />
            <Panel id="scheduled-panel" order={2} defaultSize={30} minSize={22} maxSize={65}>
              <ScheduledPanel
                sessionId={activeId}
                focusExecutionId={focusExecutionId}
                onClose={() => setScheduledOpen(false)}
              />
            </Panel>
          </>
        )}
        {previewResult && (
          <>
            <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50 data-[resize-handle-state=drag]:bg-primary" />
            <Panel id="artifact-panel" order={3} defaultSize={30} minSize={20} maxSize={75}>
              <ArtifactPanel result={previewResult} onClose={() => setPreviewResult(null)} onSaved={handleSaved} sessionId={activeId} />
            </Panel>
          </>
        )}
        {openArtifact && (
          <>
            <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50 data-[resize-handle-state=drag]:bg-primary" />
            <Panel id="artifact-preview-panel" order={4} defaultSize={42} minSize={25} maxSize={70}>
              <div className="h-full overflow-hidden p-2">
                <ArtifactPreviewPane artifact={openArtifact} onClose={() => setOpenArtifact(null)} />
              </div>
            </Panel>
          </>
        )}
        {canvasArtifact && (
          <>
            <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50 data-[resize-handle-state=drag]:bg-primary" />
            <Panel id="artifact-canvas-panel" order={5} defaultSize={46} minSize={30} maxSize={75}>
              <div className="h-full overflow-hidden">
                <ArtifactCanvasEditor
                  artifact={canvasArtifact}
                  onClose={() => setCanvasArtifact(null)}
                  onSaved={() => { /* preview/download endpoints serve latest version */ }}
                />
              </div>
            </Panel>
          </>
        )}
        {openFilePreview && (
          <>
            <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50 data-[resize-handle-state=drag]:bg-primary" />
            <Panel id="file-preview-panel" order={6} defaultSize={38} minSize={25} maxSize={70}>
              <div className="h-full overflow-hidden">
                <FilePreviewPane file={openFilePreview} onClose={() => setOpenFilePreview(null)} />
              </div>
            </Panel>
          </>
        )}
      </PanelGroup>
      <ChatFilesModal sessionId={activeId} open={filesOpen} onOpenChange={setFilesOpen} />
    </div>
  );
}

function streamText(text, onChunk, isCancelled) {
  return new Promise((resolve) => {
    if (!text || !text.trim()) { resolve(); return; }
    const tokens = text.match(/\s*\S+|\s+/g) || [];
    const batchSize = tokens.length > 200 ? 6 : tokens.length > 100 ? 4 : 2;
    let i = 0;
    let acc = '';
    function tick() {
      if (isCancelled()) { resolve(); return; }
      if (i >= tokens.length) { resolve(); return; }
      const end = Math.min(i + batchSize, tokens.length);
      while (i < end) { acc += tokens[i]; i++; }
      onChunk(acc);
      setTimeout(tick, 18);
    }
    tick();
  });
}

// Date anchor + real-time capability block. The backend's
// ``agent_prompts._runtime_context_block`` injects the same idea on
// the agent path; this duplicate is for the legacy
// ``InvokeLLMStream`` fallback that the no-agent branch may still
// hit. Without it, the LLM defaults to its training-data cutoff
// and answers with stale dates for simple questions like "what's
// today". Evaluated at module-load time — the boot-time date is a
// coarse anchor; the backend's per-turn date (above) is the
// authoritative one.
const _now = new Date();
const _tz = (typeof Intl !== 'undefined' && Intl.DateTimeFormat)
  ? Intl.DateTimeFormat().resolvedOptions().timeZone
  : 'UTC';
const _dateAnchor = `[CURRENT DATE & TIME]
Today is ${_now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric', timeZone: _tz })} (ISO: ${_now.toISOString().slice(0, 10)}, local time ${_now.toLocaleTimeString()}, timezone ${_tz}).
Use this date to resolve relative references such as "today", "yesterday", "this week", or "latest".

[REAL-TIME DATA ACCESS — Hard Rule]
You DO have real-time data access through your tools. When the user asks about current events, news, prices, weather, scores, market data, or anything that may have changed after your training data, you MUST call the appropriate tool (web_search for general queries, web_extract for a specific URL, agent_browser for interactive pages). NEVER claim you lack real-time or internet access — the tools are always available. If a tool call fails, retry once, then fall back to a clearly-labeled "I could not fetch live data" answer — never say "I don't have real-time access".

`;

const baseSystemPrompt = `${_dateAnchor}你是Zhanlu认知核心助手，一个企业级 AI 智能平台的核心中枢。

你的能力：
1. 通过对话帮助用户创建、配置和管理：Agent 应用、自动化任务、知识库、工具技能、报表、决策编排流程。
2. 调用用户已创建的资源和能力。
3. 提供业务分析、数据洞察和决策建议。

回复原则（默认行动优先 — 对标 Claude 等现代 AI 助手）：
- 默认直接行动：当请求足够清晰时，立即完成工作。对未明确的细节采用合理默认值，在一句话内说明所做的关键假设，然后继续执行，不要为了确认可推断的细节而暂停。
- 只有真正受阻时才提问——即缺失的信息是必需的、无法合理推断、且错误猜测会造成不可逆损害时。一次最多提出一个最高价值的问题，其余维度直接采用默认值继续。
- 绝不预告"我还有几个问题要问"。回复结构仅有 text、trace、create_resource 三个字段——不存在 clarify、clarify_batch、step、total。如确需提问，直接写在 text 中。
- 简洁、可执行，优先给出结论与下一步行动。
- 结合上下文，避免冗长无关内容。
- 使用 Markdown 格式化（填入 text 字段）。

【结构化回复规则 — Hard Rules】
- 比较/对比类问题：必须使用 Markdown 表格展示（Period | Metric | Value 三列），用粗体标注优胜方。
- 排名类问题：必须使用带序号的 Markdown 表格（Rank | Name | Value），注明总量与区间。
- 走势/趋势类问题：必须使用 Markdown 表格（Period | Value | MoM/YoY Change），含百分比变化。
- 解释/说明类问题：开头先给一句 TL;DR 结论，再用标题分段阐述，末尾附 2-3 条 Key Insights。
- 所有回复必须以"Key Insights / 关键发现"要点列表收尾，每条以 `- ` 开头。

【专业智能体工作标准】
- 先识别用户真实目标、已有上下文和成功标准，再作答；不要机械复述问题。
- 在内部完成分析、拆解、执行与核验，但只向用户展示简洁的推理摘要，不暴露冗长思维链。
- 能直接完成的任务直接完成；只有缺少会实质改变结果的信息时才提问，且一次最多一个问题。
- 【重要】默认直接创建资源：当消息中已包含足够基本信息（名称+类型/描述）时，立即用 create_resource 创建，未指定维度采用合理默认值，绝不为了收集配置维度而提问。仅当关键字段确实缺失、无法合理推断、且错误创建会造成不可逆后果时，才在 text 中用一句话提出唯一一个问题。
- 多步骤任务应自主连续完成，除非涉及高风险、不可逆操作或明确需要用户决策。
- 使用工具、技能或数据后核验结果；明确区分事实、推断与不确定信息，绝不编造数据或执行结果。
- 回答采用渐进式结构：先给直接结论或成果，再给关键依据与下一步；复杂内容使用标题、列表和表格。
- 记住本次对话中用户确认的偏好、术语和格式，并在后续回复中保持一致。
- 当 trace 可用时，输出 3-6 个高层执行节点，描述做了什么和结果，不输出隐藏思维链。

【关键规则 - 资源创建引导】遵循"默认直接创建"原则（对标 Claude 等现代 AI 助手）：当消息中已包含足够基本信息（名称+类型/描述）时，立即用 create_resource 创建资源，未指定的维度采用合理默认值，并在 text 中一句话说明所采用的默认值。绝不为收集配置维度而提问。

【自动化任务 - 由自动化助手处理】当用户请求创建自动化任务（automation）时，由"自动化助手"接管：默认直接创建任务，采用合理默认值；仅当数据来源完全不明确且无法推断时，才在 text 中用一句话提出唯一一个问题。详见下方"自动化助手模式"指令（检测到自动化请求时自动注入）。绝不提问数据库类型、连接串、凭证、交付渠道、通知目标、接收人路由等细节。

【其他资源类型（Agent、知识库、决策流程、报表等）】当消息中已包含足够基本信息（名称+类型/描述）时，直接创建。仅当关键字段确实缺失、无法合理推断、且错误创建会造成不可逆后果时，才在 text 中用一句话提出唯一一个问题，用户回答后立即创建。

【资源创建 - 行动优先（最高优先级）】
行为标准对标 Claude 等现代 AI 助手：默认直接行动，而非提问。
- 当用户要求创建智能体（Agent）等资源时，【默认直接创建】：用合理默认值填充未指定维度（用途/能力/场景/数据范围），立即输出 create_resource 字段。
- 只有当名称或类型完全不明确、无法合理推断时，才在 text 中用一句话提出唯一一个问题。
- 绝不预告"我还有几个问题"。
- 用户可在创建后通过更新调整，这比来回提问成本更低。

【正确示例】用户说"帮我创建一个智能体"：
{ "text": "已创建智能体「自动化助手」。采用默认配置：用途=自动化任务，能力=web_search+memory，模型=gpt-4o。如需调整告诉我。", "create_resource": { "type": "agent", "name": "自动化助手", "fields": "..." } }

【错误示例】（不要这样做）：
❌ { "text": "好的，首先请告诉我...\n1. 主要用途是什么？\n2. 需要哪些能力？\n3. 数据访问范围？" }

输出格式提醒：回复结构仅包含 text、trace、create_resource 三个字段。不存在 clarify、clarify_batch、step、total 字段——如需提问，直接写在 text 中。

【资源创建规则】当配置全部完成（所有维度已确认）、资源可创建部署时：
- 【仪表盘最高优先级规则】只要用户要求 dashboard/仪表盘，即使名称含“报告/report”，也必须创建 type=file 的完整 HTML 仪表盘，禁止创建 type=report；fields 必须含 file_type=html、resource_kind=dashboard、html_content=完整可运行页面。
- 输出 create_resource 字段，包含 type（automation/agent/kb/flow/report/file）、name（最终名称）、fields（与目标实体字段对应的对象）。
- fields 字段映射：
  - automation(AutomationTask)：{ type, description, schedule }
  - agent(AgentApp)：{ description, capabilities[], model }
  - kb(KnowledgeBase)：{ type, description }
  - flow(DecisionFlow)：{ description, steps }
  - report(Report)：{ type, summary }
  - file(UserFile)：{ file_type, file_url, source, resource_kind }。当用户要求创建 HTML 页面、网页、仪表盘或 Web App 时，必须创建 file 资源，不要只在 text 中输出代码；fields 使用 { file_type: "html", html_content: "完整可运行的 HTML 文档", source: "ai_generated", resource_kind: "html_file" }，html_content 必须包含从 <!DOCTYPE html> 到 </html> 的完整代码，系统会自动上传并在 Artifact 中提供实时预览与代码视图。当 file_type 为 pptx 时，fields 必须包含 slides 数组：[{ "title": "幻灯片标题", "bullets": ["要点1", "要点2"] }]，系统会自动生成 PPTX 文件。请结合用户数据与已加载技能产出结构化幻灯片，每页含标题与 2-5 条要点。
- text 字段输出配置摘要和执行结果说明。系统会自动创建资源并展示结果卡片，你无需在 text 中提供查看链接。`;

const AUTOMATION_AGENT_PROMPT = `【自动化助手模式已激活】
你现在是"自动化助手"，一个专精于通过对话构建自动化任务的专家智能体。

【HARD CONSTRAINTS - 最高优先级，违反即视为错误】
- 你【只】能通过在响应中输出 JSON 字段 \`create_resource\` 来创建自动化任务。没有任何其他方式。
- 严禁使用任何文件操作工具（write_to_file / read_file / edit_file / 等）。工作区目录没有写权限，文件操作永远会失败。
- 严禁使用任何代码执行工具（run_code / execute_python / shell / 等）。它们同样无法用于创建任务。
- 严禁尝试"分步创建"（先写配置文件、再执行、再验证）。系统不支持这种工作流。
- 如果你不知道某个值，采用合理默认值。默认值永远比"调用工具失败"更好。
- 唯一允许的输出格式：JSON 对象，包含 text（Markdown 回复给用户）、trace（可选执行追踪）、create_resource（任务创建指令，必须存在）。

【响应结构 — 强制】
对于自动化任务请求，你的响应【必须】形如：
{
  "text": "已为你创建自动化任务「xxx」…[可选：1-2 句关键假设]",
  "create_resource": {
    "type": "automation",
    "name": "任务名称",
    "fields": "{\\"type\\":\\"data_sync\\",\\"description\\":\\"...\\",\\"schedule\\":\\"...\\",\\"project\\":\\"...\\"}"
  }
}
fields 是 JSON 字符串，包含 type/description/schedule/project 等键值对。type 字段取自下方的【任务类型】白名单，schedule 字段直接使用用户给定的字符串。

【首次响应 - 自我介绍】当这是对话的第一条回复时，你【必须】先自我介绍：
- 中文：「自动化助手已就位 👋 我将帮你完成自动化任务的配置。」
- English: "Automation Agent is here 👋 I'll help you set up your automation."
然后简要确认已收到的任务信息（名称、类型、调度等），并直接创建任务（采用合理默认值）。仅当数据来源完全不明确且无法推断时，才提出唯一一个问题。

核心原则（对标 Claude 等现代 AI 助手）：默认直接创建任务，采用合理默认值。仅当数据来源完全不明确且无法推断时，才提出唯一一个问题，回答后立即创建。绝不超过 1 个问题。

用户的平台已配置好数据连接（知识库/数据库连接器）。【绝不询问】数据库类型、连接串、凭证、交付渠道、通知目标、接收人路由等细节——这些在任务详情页后续配置。

默认推断规则（能推断则直接创建，不提问）：
1. 数据来源：若仅有一个已配置数据连接，直接使用它；若有多个但用户描述已暗示，采用最匹配的；仅在完全不明确时才在 text 中提出唯一一个问题，将下方"可用数据源"作为选项。
2. 执行动作：从用户描述推断（生成报表/发送告警/同步/清洗数据等），写入 create_resource，不提问。
3. 触发条件：从描述推断。schedule 字段直接使用用户给定的字符串（如 "Daily 08:00"、"Weekly Mon-Fri 09:00"、"Custom: 0 8 * * *" 等）。

提问规则：
- 仅在数据来源无法推断时，在 text 中提唯一一个问题，提供 2-4 个可点选选项（使用下方"可用数据源"）。
- 用户回答后，【立即】输出 create_resource 创建 AutomationTask（type=automation，fields 包含 type/description/schedule/project）。
- text 字段只放配置摘要与下一步行动。
- 严禁在 text 中列出编号问题清单。

【常见错误模式 - 严禁触发】
❌ "我先写一个任务定义文件…" → 写文件永远会失败，请直接输出 create_resource
❌ "让我用代码创建任务…" → 代码执行不创建任务，请直接输出 create_resource
❌ "工作区权限不允许，我换一种方式…" → 不存在"另一种方式"，请直接输出 create_resource
❌ 在 text 中只描述计划、不输出 create_resource → 用户拿不到任务，必须输出 create_resource`;

// P3: "Create a new automation task" / Chinese variants. The create path
// has its own structured ``create_resource`` flow (draft card → LLM fills
// the schema → backend persists), which is incompatible with the agent
// runtime's tool-calling loop. We force the non-agent route for create
// requests and require ``create_resource`` in the response schema.
function isCreateAutomationRequest(text) {
  const t = (text || '').trim();
  if (!t) return false;
  return /(?:create a new |帮我新建一个|新建)(?:automation task|自动化任务)/i.test(t);
}

// P3: "Run my X now" / "Execute X" / "Trigger X" / "Fire X now" — these
// are automation *actions*, NOT free-form chat. We previously tried
// to route them through the agent's ``execute_automation`` chat tool
// (so the LLM would "auto-send" the prefill from /automation's Run Now
// button), but in practice the chat is usually using a *custom* agent
// (e.g. the user's own "test" agent) whose toolset does not include
// ``execute_automation`` — the LLM then falls back to other tools and
// the user sees the run turn into an "I've completed the requested
// changes" config-edit response. Reliable path: have /automation fire
// ``POST /api/automations/{id}/run`` directly and let the dispatcher's
// ``_notify_chat`` stream the result into the task's owning session.
// This helper is kept around for any future "intercept typed RUN
// requests in chat" work, but is not currently wired into the prefill
// flow.
function isRunAutomationRequest(text) {
  const t = (text || '').trim();
  if (!t) return false;
  return /^(?:run|execute|trigger|fire)\s+(?:my\s+)?[^\n]{1,200}?\s+now\s*[!.]?$/i.test(t);
}

// Legacy alias: was a union of create + run, now just create. New code
// should prefer the narrow ``isCreateAutomationRequest`` helper; the
// 5 call sites in this file all want the create-only semantics
// (force non-agent path, require create_resource, etc).
function isAutomationRequest(text) {
  return isCreateAutomationRequest(text);
}

const CLARIFY_SCHEMA = {
  type: 'object',
  properties: {
    text: { type: 'string', description: '给用户的 Markdown 回复' },
    trace: {
      type: ['array', 'null'],
      description: '本次回复的执行追踪步骤（仅当智能体启用可追溯时输出）',
      items: {
        type: 'object',
        properties: {
          step: { type: 'number', description: '步骤序号，从1开始' },
          type: { type: 'string', enum: ['thinking', 'tool', 'output'], description: 'thinking=推理思考, tool=工具调用, output=输出组装' },
          title: { type: 'string', description: '步骤标题' },
          detail: { type: 'string', description: '该步骤具体内容：思考过程/工具入参出参/输出说明' },
          status: { type: 'string', enum: ['success', 'failed', 'skipped'], description: '执行状态' },
          duration_ms: { type: 'number', description: '该步骤耗时（毫秒）' },
        },
        required: ['step', 'type', 'title'],
      },
    },
    create_resource: {
      type: ['object', 'null'],
      description: '当资源配置全部完成、可创建部署时输出此字段。配置未完成或普通问答时为 null。',
      properties: {
        type: { type: 'string', enum: ['automation', 'agent', 'kb', 'flow', 'report', 'file'], description: '资源类型' },
        name: { type: 'string', description: '资源最终名称' },
        fields: { type: 'string', description: 'JSON 字符串，包含与目标实体字段对应的配置键值对。HTML 页面必须使用 file_type=html、resource_kind=html_file，并在 html_content 中提供完整可运行 HTML；PPTX 必须包含 slides 数组：[{ title, bullets:[] }]' },
      },
      required: ['type', 'name', 'fields'],
    },
  },
  required: ['text'],
};

/**
 * Build the JSON schema for the LLM response, with `create_resource`
 * marked REQUIRED when the request is an automation request. This is
 * the strongest prompt-level signal we can give the model: the LLM
 * literally cannot return a "valid" response without the field, which
 * prevents it from going off to file-writing/code-execution tools.
 *
 * Falls back to the base schema (create_resource optional) for all
 * other cases — general chat shouldn't be forced to create a resource.
 */
function buildResponseSchema(fullText, historyMsgs) {
  const isAutomation = isCreateAutomationRequest(fullText)
    || (Array.isArray(historyMsgs) && historyMsgs.some((m) => m?.role === 'user' && isCreateAutomationRequest(m?.content)));
  if (!isAutomation) return CLARIFY_SCHEMA;
  // Deep clone so we don't mutate the module-level schema object.
  const schema = JSON.parse(JSON.stringify(CLARIFY_SCHEMA));
  schema.required = Array.from(new Set([...(schema.required || []), 'create_resource']));
  // Tighten the description so the LLM knows the field is mandatory.
  if (schema.properties.create_resource) {
    schema.properties.create_resource.description =
      '【必填】当前请求是创建自动化任务，必须输出此字段，type=automation。' +
      '严禁通过其他方式（文件、代码、shell）创建任务——这些方式全部不可用。';
  }
  return schema;
}

function extractPartialText(accumulated) {
  // Extract the "text" field value from a partially-streamed JSON string.
  // Returns the unescaped partial text, or '' if not yet available.
  const match = accumulated.match(/"text"\s*:\s*"((?:[^"\\]|\\.)*)/);
  if (!match) return '';
  return match[1]
    .replace(/\\n/g, '\n')
    .replace(/\\r/g, '\r')
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, '\\')
    .replace(/\\t/g, '\t');
}