/**
 * Chat feedback (experience layer, Phase C) — component contract tests.
 *
 * The thumbs up/down buttons live in the shared assistant bubble
 * (`MessageBubble`), which Chat.jsx wires via `onFeedback` +
 * `feedbackRating` props. These tests cover:
 *   1. buttons render on finalized assistant messages only
 *   2. clicking submits via the agentFeedback API client
 *   3. optimistic selected state driven by `feedbackRating`
 *   4. the API client builds the correct POST to the feedback endpoint
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';

// ---------------------------------------------------------------------------
// Mocks for heavy MessageBubble children
// ---------------------------------------------------------------------------
vi.mock('@/lib/LanguageProvider', () => ({
  // t must be an OBJECT (t.chat.phase, t.copy, ...), not the i18next-style
  // function mock `t: (x) => x` — that made t.chat undefined and every
  // component render crashed on `t.chat.phase` (reading 'phase' of
  // undefined). Keys mirror the real translations for the components
  // under test (MessageBubble/MessageActions).
  useLanguage: () => ({
    lang: 'en',
    t: {
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
        phase: {},
        uploadFailed: 'Upload failed',
      },
      common: { send: 'Send' },
      copy: 'Copy', share: 'Share', like: 'Like', dislike: 'Dislike',
      copyText: 'Copy Text', generateImage: 'Generate Image',
      generateDoc: 'Generate Document', cancel: 'Cancel', regenerate: 'Regenerate',
      kb: { dbTypes: {} },
    },
  }),
}));
vi.mock('@/components/chat/ClarifyOptions', () => ({ default: () => null }));
vi.mock('@/components/chat/ClarifyBatchForm', () => ({ default: () => null }));
vi.mock('@/components/chat/ActivitySteps', () => ({ default: () => null }));
vi.mock('@/components/chat/ResultCard', () => ({ default: () => null }));
vi.mock('@/components/chat/DataTableCard', () => ({ default: () => null }));
vi.mock('@/components/chat/ReportCard', () => ({ default: () => null }));
vi.mock('@/components/chat/ArtifactPreviewCard', () => ({ default: () => null }));
vi.mock('@/components/chat/ArtifactCardList', () => ({ default: () => null }));
vi.mock('@/components/chat/InlineArtifactPreview', () => ({ default: () => null }));
vi.mock('@/components/chat/PlanEditor', () => ({ default: () => null }));
vi.mock('@/components/dashboard/DashboardCard', () => ({ default: () => null }));
vi.mock('@/components/dashboard/DashboardPopup', () => ({ default: () => null }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock('@/components/ui/tooltip', () => ({
  TooltipProvider: ({ children }) => React.createElement('div', null, children),
  Tooltip: ({ children }) => React.createElement('div', null, children),
  TooltipTrigger: ({ children }) => children,
  TooltipContent: () => null,
}));
vi.mock('@/components/ui/dropdown-menu', () => ({
  DropdownMenu: ({ children }) => React.createElement('div', null, children),
  DropdownMenuTrigger: ({ children }) => children,
  DropdownMenuContent: ({ children }) => React.createElement('div', null, children),
  DropdownMenuItem: ({ children, onClick }) => React.createElement('button', { onClick }, children),
  DropdownMenuSeparator: () => null,
}));

// ---------------------------------------------------------------------------
// Module under test (loaded after mocks)
// ---------------------------------------------------------------------------
const { default: MessageBubble } = await import('@/components/chat/MessageBubble');

function makeMessage(overrides = {}) {
  return {
    id: 'm-1',
    role: 'assistant',
    content: '今日价格 9800 元/吨。',
    ...overrides,
  };
}

function renderBubble({ message, isStreaming = false, onFeedback, feedbackRating = null }) {
  return render(
    React.createElement(MessageBubble, {
      message,
      isStreaming,
      onFeedback,
      feedbackRating,
    }),
  );
}

describe('MessageBubble feedback buttons', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders thumbs up/down on a finalized assistant message', () => {
    renderBubble({ message: makeMessage(), onFeedback: vi.fn() });
    expect(screen.getByLabelText('Like')).toBeTruthy();
    expect(screen.getByLabelText('Dislike')).toBeTruthy();
  });

  it('does not render on user messages', () => {
    renderBubble({ message: makeMessage({ role: 'user' }), onFeedback: vi.fn() });
    expect(screen.queryByLabelText('Like')).toBeNull();
    expect(screen.queryByLabelText('Dislike')).toBeNull();
  });

  it('does not render while streaming', () => {
    renderBubble({ message: makeMessage(), isStreaming: true, onFeedback: vi.fn() });
    expect(screen.queryByLabelText('Like')).toBeNull();
    expect(screen.queryByLabelText('Dislike')).toBeNull();
  });

  it('does not render like/dislike without an onFeedback handler', () => {
    renderBubble({ message: makeMessage() });
    expect(screen.queryByLabelText('Like')).toBeNull();
    expect(screen.queryByLabelText('Dislike')).toBeNull();
  });

  it('clicking thumbs up calls onFeedback with (id, 1)', () => {
    const onFeedback = vi.fn();
    renderBubble({ message: makeMessage(), onFeedback });
    fireEvent.click(screen.getByLabelText('Like'));
    expect(onFeedback).toHaveBeenCalledWith('m-1', 1);
  });

  it('clicking thumbs down calls onFeedback with (id, -1)', () => {
    const onFeedback = vi.fn();
    renderBubble({ message: makeMessage(), onFeedback });
    fireEvent.click(screen.getByLabelText('Dislike'));
    expect(onFeedback).toHaveBeenCalledWith('m-1', -1);
  });

  it('applies selected state from feedbackRating (optimistic UI)', () => {
    const { rerender } = renderBubble({ message: makeMessage(), onFeedback: vi.fn(), feedbackRating: 1 });
    expect(screen.getByLabelText('Like').className).toContain('text-green-600');
    rerender(
      React.createElement(MessageBubble, {
        message: makeMessage(),
        onFeedback: vi.fn(),
        feedbackRating: -1,
      }),
    );
    expect(screen.getByLabelText('Dislike').className).toContain('text-red-600');
  });

  it('always renders copy and share buttons on finalized assistant messages', () => {
    renderBubble({ message: makeMessage() });
    expect(screen.getByLabelText('Copy')).toBeTruthy();
    expect(screen.getByLabelText('Share')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// API client contract
// ---------------------------------------------------------------------------
describe('postMessageFeedback', () => {
  it('POSTs rating to the feedback endpoint and returns the body', async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, rating: 1, message_id: 'm-1' }),
    });
    vi.doMock('@/api/authFetch', () => ({ authFetch: mockFetch }));
    // Force a fresh module load so doMock takes effect
    const mod = await import('@/api/agentFeedback?update=' + Date.now());
    const out = await mod.postMessageFeedback('app-1', 'conv-1', 'm-1', 1, 'nice report');

    expect(mockFetch).toHaveBeenCalledWith(
      '/api/apps/app-1/agents/conversations/conv-1/messages/m-1/feedback',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ rating: 1, comment: 'nice report' }),
      }),
    );
    expect(out.rating).toBe(1);
    vi.doUnmock('@/api/authFetch');
  });

  it('throws on non-ok response', async () => {
    // Clear module cache so doMock takes effect with the failing fetch
    vi.resetModules();
    const mockFetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ error: 'message not found' }),
    });
    vi.doMock('@/api/authFetch', () => ({ authFetch: mockFetch }));
    const { postMessageFeedback: pmf } = await import('@/api/agentFeedback');

    await expect(pmf('app-1', 'conv-1', 'm-1', -1)).rejects.toThrow('message not found');
    vi.doUnmock('@/api/authFetch');
  });
});
