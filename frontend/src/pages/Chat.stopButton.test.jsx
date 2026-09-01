/**
 * Chat.jsx — Stop button visibility contract (integration test).
 *
 * The Stop button is rendered by ChatInput when isStreaming is true. The
 * isStreaming prop is wired to `streamState.isActive`, which is true only
 * while state is 'sending' or 'streaming'. The fix is in handleAgentSend:
 * the for-await loop's `done` event handler MUST call `streamState.complete()`
 * so the state transitions out of 'streaming' and the Stop button hides.
 *
 * Without the fix, the state stays at 'streaming' and the Stop button remains
 * visible after the agent finishes.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import React from 'react';

// jsdom doesn't implement scrollTo
beforeAll(() => {
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = vi.fn();
  }
});

// ---------------------------------------------------------------------------
// Seed data (hoisted so vi.mock factories can reference it)
// ---------------------------------------------------------------------------
const { seedSession, seedMessages, genStream, latestIsStreaming } = vi.hoisted(() => ({
  seedSession: [
    { id: 'sess-1', title: 'StopBtn Test', project: 'Default', last_message_at: '2026-07-15T12:00:00Z' },
  ],
  seedMessages: [],
  genStream: vi.fn(),
  latestIsStreaming: { value: false },
}));

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/api/base44Client', () => ({
  base44: {
    entities: {
      ChatSession: {
        list: vi.fn().mockResolvedValue(seedSession),
        create: vi.fn().mockResolvedValue({ id: 'sess-new', project: 'Default' }),
        update: vi.fn().mockResolvedValue({}),
        delete: vi.fn().mockResolvedValue({}),
      },
      ChatMessage: {
        filter: vi.fn().mockResolvedValue(seedMessages),
        create: vi.fn().mockResolvedValue({ id: 'msg-new' }),
        update: vi.fn().mockResolvedValue({}),
        deleteMany: vi.fn().mockResolvedValue({}),
      },
      KnowledgeBase: { list: vi.fn().mockResolvedValue([]) },
      AgentApp: { get: vi.fn().mockResolvedValue(null), list: vi.fn().mockResolvedValue([]) },
      // Chat.jsx's ?conv= rehydration path calls AgentConversation.get();
      // without this entity the effect throws before rendering.
      AgentConversation: {
        get: vi.fn().mockResolvedValue(null),
        update: vi.fn().mockResolvedValue({}),
      },
      Report: { update: vi.fn().mockResolvedValue({}) },
      UserFile: { create: vi.fn().mockResolvedValue({}) },
    },
    integrations: {
      Core: {
        InvokeLLMStream: vi.fn(),
        InvokeLLM: vi.fn(),
        UploadFile: vi.fn().mockResolvedValue({ file_url: 'http://example.com/f.png' }),
      },
    },
    functions: { invoke: vi.fn() },
  },
}));

vi.mock('@/lib/app-params', () => ({
  appParams: { appId: 'test-app-id' },
}));

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({
    t: {
      sessionList: { ungrouped: 'Default', newSession: 'New' },
      chat: {
        coreTitle: 'Chat',
        categories: { production: {}, maintenance: {}, quality: {}, safety: {}, supply: {}, energy: {} },
        chatFiles: { title: 'Files' },
        related: {},
      },
      kb: { dbTypes: {} },
    },
    lang: 'en',
  }),
}));

vi.mock('@/lib/useTranslate', () => ({
  useTranslate: () => (text) => text,
}));

vi.mock('@/lib/skillContext', () => ({
  buildSkillContext: () => '',
  buildDefaultSkillContext: () => '',
}));

vi.mock('@/api/agentEnhanced', () => ({
  streamAgentResponse: (...args) => genStream(...args),
  createAgentConversation: vi.fn().mockResolvedValue({ id: 'conv-1' }),
  getSessionMessages: vi.fn().mockResolvedValue(seedMessages),
}));

vi.mock('@/lib/intentClassifier', () => ({
  classifyIntent: () => ({}),
  formatHint: () => '',
}));

vi.mock('@/lib/detectLang', () => ({
  detectLang: () => 'en',
}));

// Mock ChatInput to expose a send button, agent-pick button, and surface the
// isStreaming prop
vi.mock('@/components/chat/ChatInput', () => ({
  default: ({ isStreaming, onSend, onStop, value, onChange, onSelectAgent, activeAgent }) => {
    latestIsStreaming.value = isStreaming;
    return React.createElement(
      'div',
      { 'data-testid': 'chat-input', 'data-streaming': String(!!isStreaming) },
      React.createElement('input', {
        'data-testid': 'msg-input',
        value: value || '',
        onChange: (e) => onChange?.(e.target.value),
      }),
      activeAgent
        ? React.createElement(
            'span',
            { 'data-testid': 'active-agent-badge' },
            `agent:${activeAgent.name || activeAgent.id || 'unknown'}`
          )
        : React.createElement(
            'button',
            {
              'data-testid': 'btn-pick-agent',
              onClick: () => onSelectAgent?.({ id: 'agent-1', name: 'test_agent' }),
            },
            'Pick Agent'
          ),
      isStreaming
        ? React.createElement(
            'button',
            { 'data-testid': 'btn-stop', onClick: () => onStop?.() },
            'Stop'
          )
        : React.createElement(
            'button',
            { 'data-testid': 'btn-send', onClick: () => onSend?.('hello agent') },
            'Send'
          )
    );
  },
}));

// Mock other heavy children to simple divs
vi.mock('@/components/chat/SessionList', () => ({
  default: () => React.createElement('div', { 'data-testid': 'session-list' }),
}));
vi.mock('@/components/chat/MessageBubble', () => ({
  default: () => React.createElement('div', { 'data-testid': 'message-bubble' }),
}));
vi.mock('@/components/chat/RelatedContent', () => ({
  default: () => React.createElement('div', { 'data-testid': 'related-content' }),
}));
vi.mock('@/components/chat/ArtifactPanel', () => ({
  default: () => React.createElement('div', { 'data-testid': 'artifact-panel' }),
}));
vi.mock('@/components/chat/ArtifactPreviewPane', () => ({
  default: () => React.createElement('div', { 'data-testid': 'artifact-preview-pane' }),
}));
vi.mock('@/components/chat/ReportSidePanel', () => ({
  default: () => React.createElement('div', { 'data-testid': 'report-side-panel' }),
}));
vi.mock('@/components/chat/ChatFilesModal', () => ({
  default: () => React.createElement('div', { 'data-testid': 'chat-files-modal' }),
}));
vi.mock('@/components/chat/ScheduledPanel', () => ({
  default: () => React.createElement('div', { 'data-testid': 'scheduled-panel' }),
}));
vi.mock('react-resizable-panels', () => {
  const Panel = ({ children, ...props }) =>
    React.createElement('div', { 'data-testid': `panel-${props.id || 'unknown'}`, ...props }, children);
  const PanelGroup = ({ children, ...props }) =>
    React.createElement('div', { 'data-testid': 'panel-group', ...props }, children);
  const PanelResizeHandle = () => React.createElement('div', { 'data-testid': 'panel-resize-handle' });
  return { Panel, PanelGroup, PanelResizeHandle };
});
vi.mock('@/lib/utils', () => ({
  cn: (...args) => args.filter(Boolean).join(' '),
}));

// ---------------------------------------------------------------------------
// Helper: build a fake streamAgentResponse that yields a given sequence
// then hangs (so the test can assert the state at any point).
// ---------------------------------------------------------------------------
function makeStream(events) {
  return vi.fn(async function* () {
    for (const evt of events) {
      // Yield across a MACROTASK boundary so React flushes the
      // intermediate streaming state. Without this, the sync mock
      // generator runs start→done inside a single microtask drain and
      // the transient btn-stop (isStreaming=true) is never rendered —
      // React batches all state updates until the macrotask boundary.
      // A real SSE socket has natural macrotask gaps between events.
      await new Promise((r) => setTimeout(r, 0));
      yield evt;
    }
    // After emitting all events, hang forever — simulates the SSE socket
    // staying open. The safety net (watchdog / line-778 reset) will then
    // need to fire for the state to transition.
    await new Promise(() => {});
  });
}

// ---------------------------------------------------------------------------
// Dynamic import after mocks
// ---------------------------------------------------------------------------
const { default: Chat } = await import('@/pages/Chat');
const { ChatSessionProvider } = await import('@/lib/ChatSessionContext');
const { PersistentStreamProvider } = await import('@/lib/PersistentStreamContext');

function renderChat() {
  // Chat.jsx now reads session state from ChatSessionContext (the
  // provider was hoisted out of Chat.jsx in the unified-sidebar
  // refactor — Option A). And useNavigate() requires a <Router>
  // ancestor. Wrap the page in both so the test can drive Chat in
  // isolation, without mounting the full AppLayout.
  return render(
    React.createElement(
      MemoryRouter,
      null,
      React.createElement(
        ChatSessionProvider,
        null,
        React.createElement(PersistentStreamProvider, null, React.createElement(Chat)),
      ),
    )
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Chat — Stop button visibility after agent stream emits `done`', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    latestIsStreaming.value = false;
  });

  it('shows the Stop button while streaming and hides it after a `done` event (BUG REGRESSION)', async () => {
    genStream.mockImplementation(
      makeStream([
        { type: 'delta', content: 'Hello ' },
        { type: 'delta', content: 'world' },
        { type: 'done', content: 'Hello world', tokens: { total: 5 } },
      ])
    );

    renderChat();

    // Pick an agent so the agent path (handleAgentSend) is taken.
    await waitFor(() => expect(screen.getByTestId('btn-pick-agent')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('btn-pick-agent'));
    await waitFor(() => expect(screen.getByTestId('active-agent-badge')).toBeInTheDocument());

    // Click Send to start the agent stream
    fireEvent.click(screen.getByTestId('btn-send'));

    // Stop button should appear while streaming
    await waitFor(() => expect(screen.getByTestId('btn-stop')).toBeInTheDocument(), { timeout: 2000 });

    // After the `done` event is processed, the Stop button MUST be hidden
    // and the Send button MUST reappear. With the bug, isStreaming stays
    // true because streamState.complete() is never called.
    await waitFor(() => expect(screen.getByTestId('btn-send')).toBeInTheDocument(), { timeout: 2000 });

    // Belt-and-suspenders: the ChatInput wrapper's data-streaming attribute
    // must reflect the latest state.
    expect(screen.getByTestId('chat-input').getAttribute('data-streaming')).toBe('false');
    expect(latestIsStreaming.value).toBe(false);
  });

  it('hides the Stop button after a `done` event even when the SSE stream hangs after done', async () => {
    // Same as above but the stream also hangs after `done` (realistic — many
    // SSE backends keep the socket open after the last event). The done
    // handler must transition the state machine so the Stop button hides.
    genStream.mockImplementation(
      makeStream([
        { type: 'delta', content: 'Partial' },
        { type: 'done', content: 'Partial', tokens: { total: 1 } },
      ])
    );

    renderChat();
    await waitFor(() => expect(screen.getByTestId('btn-pick-agent')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('btn-pick-agent'));
    await waitFor(() => expect(screen.getByTestId('active-agent-badge')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('btn-send'));

    await waitFor(() => expect(screen.getByTestId('btn-stop')).toBeInTheDocument(), { timeout: 2000 });
    await waitFor(() => expect(screen.getByTestId('btn-send')).toBeInTheDocument(), { timeout: 2000 });
    expect(latestIsStreaming.value).toBe(false);
  });
  // NOTE (2026-08-31): the old "30s watchdog" test was REMOVED — the
  // auto-reset watchdog no longer exists in Chat.jsx (the current design
  // keeps the Stop button visible until the user clicks it or the stream
  // errors, instead of force-resetting after a fixed timeout). The test
  // pinned behavior that was deliberately deleted.
});
