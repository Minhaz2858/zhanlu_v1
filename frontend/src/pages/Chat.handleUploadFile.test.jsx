/**
 * Regression test for the file-upload chip bug (2026-08-14).
 *
 * Bug: clicking `+` → Upload, picking a file, and the upload succeeding
 * at the byte level but failing at *persistence* (`ensureSession` /
 * `UserFile.create`) left NO attachment chip in the composer. The old
 * `handleUploadFile` called `setAttachments` only AFTER both persistence
 * steps — so any throw in those steps silently dropped the chip and the
 * user saw nothing (no toast either).
 *
 * Fix: `handleUploadFile` now calls `setAttachments` IMMEDIATELY after
 * `UploadFile` returns a `file_url`, before any `ensureSession` /
 * `UserFile.create` call. Persistence failures are caught and logged but
 * no longer hide the chip.
 *
 * This test pins that contract: the chip MUST appear even when
 * `UserFile.create` rejects.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import React from 'react';

// jsdom doesn't implement scrollTo — Chat.jsx calls scrollRef.current.scrollTo()
beforeAll(() => {
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = vi.fn();
  }
});

// Captured `onUploadFile` prop from the (mocked) ChatInput so the test
// can drive a real upload through the component's own handler.
let capturedOnUploadFile = null;

const { seedSession } = vi.hoisted(() => ({
  seedSession: [{ id: 'sess-1', title: 'Test Chat', project: 'Default', last_message_at: '2026-07-15T12:00:00Z' }],
}));

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
        filter: vi.fn().mockResolvedValue([]),
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
        UploadFile: vi.fn().mockResolvedValue({ file_url: 'http://example.com/f.txt' }),
      },
    },
    functions: { invoke: vi.fn() },
    auth: { me: vi.fn(), setToken: vi.fn() },
  },
}));

vi.mock('@/lib/app-params', () => ({ appParams: { appId: 'test-app-id' } }));

const mockT = {
  sessionList: { ungrouped: 'Default', newSession: 'New' },
  chat: {
    coreTitle: 'Chat',
    categories: {
      production: { label: 'Production' },
      maintenance: { label: 'Maintenance' },
      quality: { label: 'Quality' },
      safety: { label: 'Safety' },
      supply: { label: 'Supply' },
      energy: { label: 'Energy' },
    },
    chatFiles: { title: 'Files' },
    related: {},
    uploadFailed: 'Upload failed',
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

vi.mock('@/lib/detectLang', () => ({ detectLang: () => 'en' }));

vi.mock('@/components/chat/SessionList', () => ({
  default: () => React.createElement('div', { 'data-testid': 'session-list' }),
}));

// ChatInput mock: render the attachment file names so the test can assert
// the chip appeared, and capture the `onUploadFile` prop so the test can
// drive a real upload through the component's own handler.
vi.mock('@/components/chat/ChatInput', () => ({
  default: (props) => {
    capturedOnUploadFile = props.onUploadFile;
    return React.createElement(
      'div',
      { 'data-testid': 'chat-input' },
      (props.attachments || []).map((a, i) =>
        React.createElement('span', { key: i, 'data-testid': 'attachment-chip' }, a.name),
      ),
    );
  },
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

const { default: Chat } = await import('@/pages/Chat');
const { ChatSessionProvider } = await import('@/lib/ChatSessionContext');
const { PersistentStreamProvider } = await import('@/lib/PersistentStreamContext');
const { base44 } = await import('@/api/base44Client');

function renderChat() {
  return render(
    React.createElement(
      MemoryRouter,
      null,
      React.createElement(
        ChatSessionProvider,
        null,
        React.createElement(PersistentStreamProvider, null, React.createElement(Chat)),
      ),
    ),
  );
}

describe('Chat — handleUploadFile renders chip even if persistence fails', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedOnUploadFile = null;
    // UploadFile always succeeds (byte-level upload OK).
    base44.integrations.Core.UploadFile.mockResolvedValue({ file_url: 'http://example.com/x.txt' });
    base44.entities.ChatSession.create.mockResolvedValue({ id: 'sess-1' });
  });

  it('renders the attachment chip even when UserFile.create rejects', async () => {
    // Persistence fails AFTER the successful byte upload.
    base44.entities.UserFile.create.mockRejectedValue(new Error('boom'));

    renderChat();

    await waitFor(() => expect(capturedOnUploadFile).toBeInstanceOf(Function));

    const file = new File(['hello'], 'test.txt', { type: 'text/plain' });
    await capturedOnUploadFile(file);

    // The chip MUST appear because setAttachments fires before persistence.
    await waitFor(() => {
      const chips = screen.getAllByTestId('attachment-chip');
      expect(chips.some((c) => c.textContent === 'test.txt')).toBe(true);
    });
  });

  it('surfaces a toast and renders NO chip when UploadFile itself fails', async () => {
    base44.integrations.Core.UploadFile.mockRejectedValue(new Error('network down'));

    renderChat();

    await waitFor(() => expect(capturedOnUploadFile).toBeInstanceOf(Function));

    const file = new File(['hello'], 'test.txt', { type: 'text/plain' });
    await capturedOnUploadFile(file);

    // No chip because the upload never produced a file_url.
    await waitFor(() => {
      expect(screen.queryByTestId('attachment-chip')).not.toBeInTheDocument();
    });
  });

  it('keeps the chip when the upload creates a brand-new session (landing-page flow)', async () => {
    // Regression (2026-08-31): on the landing page activeId is null, so
    // ensureSession() creates a NEW session mid-upload. The old
    // session-change effect unconditionally wiped draft attachments on
    // activeId change — the chip the user just uploaded vanished a tick
    // later and the agent never received the file. The effect must skip
    // the wipe for brand-new sessions (the same gate it already uses
    // for setActiveAgent).
    base44.entities.ChatSession.create.mockResolvedValue({ id: 'sess-brand-new', project: 'Default' });

    renderChat();

    await waitFor(() => expect(capturedOnUploadFile).toBeInstanceOf(Function));

    const file = new File(['hello'], 'new-session.txt', { type: 'text/plain' });
    await capturedOnUploadFile(file);

    // The chip must survive the session creation + activeId change.
    await waitFor(() => {
      const chips = screen.getAllByTestId('attachment-chip');
      expect(chips.some((c) => c.textContent === 'new-session.txt')).toBe(true);
    });
  });
});
