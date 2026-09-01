/**
 * Chat page tests — focused on the right-anchored artifact preview pane
 * lifecycle (opens on onArtifactPreview, closes on new session / switch).
 *
 * Because Chat.jsx is a heavy top-level component, we mock all
 * dependencies and seed base44 mocks with a session + messages so
 * the chat renders the message list (which includes MessageBubble).
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import React from 'react';

// jsdom doesn't implement scrollTo — Chat.jsx calls scrollRef.current.scrollTo()
beforeAll(() => {
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = vi.fn();
  }
});

// ---------------------------------------------------------------------------
// Seed data — must be defined before mocks via vi.hoisted
// ---------------------------------------------------------------------------

const { seedSession, seedMessages } = vi.hoisted(() => ({
  seedSession: [{ id: 'sess-1', title: 'Test Chat', project: 'Default', last_message_at: '2026-07-15T12:00:00Z' }],
  seedMessages: [
    { id: 'msg-1', session_id: 'sess-1', role: 'user', content: 'Tell me about my database', order: 0 },
    { id: 'msg-2', session_id: 'sess-1', role: 'assistant', content: 'Here is your report', order: 1,
      tool_calls: [
        {
          name: 'ask_data_agent',
          results: {
            type: 'report_card',
            report_card_payload: {
              title: 'Test Report', source: 'MySQL', generated_at: '2026-07-15T12:00:00Z',
              kpis: [{ label: 'Revenue', value: 1000000 }],
              chart: { type: 'bar', data: [{ month: 'Jan', Revenue: 100 }] },
              insights: [{ text: 'Revenue is growing', icon: 'trending_up' }],
              actions: [{ label: 'Show more', prompt: 'show more details' }],
            },
            artifact_id: 'art-1',
            user_signal: 'default',
          },
        },
      ],
    },
  ],
}));

// ---------------------------------------------------------------------------
// Mocks — every heavyweight import used by Chat.jsx
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
      AgentApp: { get: vi.fn().mockResolvedValue(null) },
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

const mockT = {
  sessionList: { ungrouped: 'Default', newSession: 'New' },
  chat: {
    coreTitle: 'Chat',
    categories: { production: { label: 'Production' }, maintenance: { label: 'Maintenance' }, quality: { label: 'Quality' }, safety: { label: 'Safety' }, supply: { label: 'Supply' }, energy: { label: 'Energy' } },
    chatFiles: { title: 'Files' },
    related: {},
  },
  kb: { dbTypes: {} },
};

vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ t: mockT, lang: 'en' }),
}));

vi.mock('@/lib/useTranslate', () => ({
  useTranslate: () => (text) => text,
}));

vi.mock('@/lib/skillContext', () => ({
  buildSkillContext: () => '',
  buildDefaultSkillContext: () => '',
}));

vi.mock('@/api/agentEnhanced', () => ({
  streamAgentResponse: vi.fn(),
  createAgentConversation: vi.fn().mockResolvedValue({ id: 'conv-1' }),
}));

vi.mock('@/lib/intentClassifier', () => ({
  classifyIntent: () => ({}),
  formatHint: () => '',
}));

vi.mock('@/lib/detectLang', () => ({
  detectLang: () => 'en',
}));

// ---------------------------------------------------------------------------
// Child component mocks
// ---------------------------------------------------------------------------

vi.mock('@/components/chat/SessionList', () => ({
  // Note: this mock is kept for backward compat but is no longer used
  // directly by Chat.jsx (Option A: unified sidebar moved SessionList
  // into Sidebar.jsx, where it now reads from ChatSessionContext).
  // The real test driver is the <TestControls/> component below.
  default: () => React.createElement('div', { 'data-testid': 'session-list' }),
}));

vi.mock('@/components/chat/ChatInput', () => ({
  default: () => React.createElement('div', { 'data-testid': 'chat-input' }),
}));

vi.mock('@/components/chat/MessageBubble', () => ({
  default: ({ onArtifactPreview }) => {
    const children = onArtifactPreview
      ? [React.createElement('button', {
          'data-testid': 'btn-open-artifact-preview',
          onClick: () => onArtifactPreview({
            id: 'art-1',
            type: 'docx',
            title: 'Test Report',
            file_name: 'Test_Report.docx',
            preview_url: '/api/artifacts/art-1/preview?format=html',
            file_url: '/api/artifacts/art-1/download',
            has_preview: true,
            file_size: 37000,
          }),
        }, 'Preview')]
      : [];
    return React.createElement('div', { 'data-testid': 'message-bubble' }, ...children);
  },
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
  const PanelResizeHandle = (props) =>
    React.createElement('div', { 'data-testid': 'panel-resize-handle', ...props });
  return { Panel, PanelGroup, PanelResizeHandle };
});

vi.mock('@/lib/utils', () => ({
  cn: (...args) => args.filter(Boolean).join(' '),
}));

// ---------------------------------------------------------------------------
// Dynamic import after mocks
// ---------------------------------------------------------------------------

const { default: Chat } = await import('@/pages/Chat');
const { ChatSessionProvider, useChatSession } = await import('@/lib/ChatSessionContext');
const { PersistentStreamProvider } = await import('@/lib/PersistentStreamContext');
const { base44 } = await import('@/api/base44Client');

/**
 * TestControls — replaces the inline SessionList buttons that used to live
 * in Chat.jsx. The previous test relied on a SessionList mock that fired
 * `onSelect` / `onNew` callbacks; with the unified sidebar refactor
 * (Option A), those callbacks no longer exist — SessionList (now inside
 * Sidebar) drives selection through the ChatSessionContext directly.
 *
 * This component lives next to Chat in the test tree and uses the same
 * context the real sidebar uses, so the test exercises the real
 * selectSession / newChat paths (which set activeId, clear messages,
 * reset streams, etc.) without needing to mount the full Sidebar.
 */
function TestControls() {
  // Toggle between two session ids so "Select Session" can drive both
  // the "first select loads messages" and "switch to another session"
  // cases. With the unified sidebar refactor, switching sessions is
  // detected by activeId actually changing — selecting the same id
  // twice is a no-op, so the test would otherwise never exercise the
  // "switch resets preview pane" path.
  const { selectSession, newChat, setActiveId, activeId } = useChatSession();
  const next = activeId === 'sess-1' ? 'sess-2' : 'sess-1';
  return React.createElement(
    'div',
    { 'data-testid': 'test-controls' },
    React.createElement('button', {
      'data-testid': 'btn-new-session',
      onClick: () => { setActiveId(null); newChat('Default'); },
    }, 'New Session'),
    React.createElement('button', {
      'data-testid': 'btn-select-session',
      onClick: () => selectSession(next),
    }, 'Select Session'),
  );
}

function renderChat() {
  return render(
    React.createElement(
      MemoryRouter,
      null,
      React.createElement(
        ChatSessionProvider,
        null,
        React.createElement(PersistentStreamProvider, null, React.createElement(Chat)),
        React.createElement(TestControls),
      ),
    )
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Chat — artifact preview pane integration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // 1. Pane does NOT auto-open on initial render
  it('does NOT render the preview pane on initial render (no auto-open)', async () => {
    renderChat();
    await waitFor(() => {
      expect(screen.queryByTestId('artifact-preview-pane')).not.toBeInTheDocument();
    });
  });

  // 2. Pane opens when MessageBubble fires onArtifactPreview
  it('opens the preview pane when MessageBubble fires onArtifactPreview', async () => {
    renderChat();
    // Select the seeded session to trigger message loading
    fireEvent.click(screen.getByTestId('btn-select-session'));

    // Wait for MessageBubbles to appear
    await waitFor(() => {
      const bubbles = screen.getAllByTestId('message-bubble');
      expect(bubbles.length).toBeGreaterThanOrEqual(1);
    });

    // Click the "Preview" button rendered by the MessageBubble mock
    const openBtns = screen.getAllByTestId('btn-open-artifact-preview');
    fireEvent.click(openBtns[0]);

    await waitFor(() => {
      expect(screen.getByTestId('artifact-preview-pane')).toBeInTheDocument();
    });
  });

  // 3. Pane closes on new session
  it('closes the preview pane when a new session is created', async () => {
    renderChat();

    fireEvent.click(screen.getByTestId('btn-select-session'));
    await waitFor(() => {
      const bubbles = screen.getAllByTestId('message-bubble');
      expect(bubbles.length).toBeGreaterThanOrEqual(1);
    });

    const openBtns = screen.getAllByTestId('btn-open-artifact-preview');
    fireEvent.click(openBtns[0]);
    await waitFor(() => {
      expect(screen.getByTestId('artifact-preview-pane')).toBeInTheDocument();
    });

    // Click "New Session" — should reset all state including the pane
    fireEvent.click(screen.getByTestId('btn-new-session'));

    await waitFor(() => {
      expect(screen.queryByTestId('artifact-preview-pane')).not.toBeInTheDocument();
    });
  });

  // 5. Message loading sorts by created_date (chronological), NOT by the
  // legacy `order` column — `order` mixes tiny sequential indexes (chat
  // turns) with epoch timestamps (scheduled-run updates from
  // _notify_chat), which grouped user inputs and agent activity into
  // separate regions instead of interleaving them by time.
  it('loads session messages sorted by created_date (chronological)', async () => {
    renderChat();
    fireEvent.click(screen.getByTestId('btn-select-session'));
    await waitFor(() => {
      expect(base44.entities.ChatMessage.filter).toHaveBeenCalledWith(
        { session_id: expect.any(String) },
        'created_date',
        200,
      );
    });
    expect(base44.entities.ChatMessage.filter).not.toHaveBeenCalledWith(
      expect.anything(),
      'order',
      expect.anything(),
    );
  });

  // 4. Pane closes on session switch
  it('closes the preview pane when switching to another session', async () => {
    renderChat();

    fireEvent.click(screen.getByTestId('btn-select-session'));
    await waitFor(() => {
      const bubbles = screen.getAllByTestId('message-bubble');
      expect(bubbles.length).toBeGreaterThanOrEqual(1);
    });

    const openBtns = screen.getAllByTestId('btn-open-artifact-preview');
    fireEvent.click(openBtns[0]);
    await waitFor(() => {
      expect(screen.getByTestId('artifact-preview-pane')).toBeInTheDocument();
    });

    // Click "Select Session" again — simulates switching session
    fireEvent.click(screen.getByTestId('btn-select-session'));

    await waitFor(() => {
      expect(screen.queryByTestId('artifact-preview-pane')).not.toBeInTheDocument();
    });
  });
});
