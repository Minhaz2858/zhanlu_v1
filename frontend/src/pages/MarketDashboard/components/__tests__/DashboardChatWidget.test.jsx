import { describe, it, expect, vi, beforeAll } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';

beforeAll(() => {
  if (!Element.prototype.scrollTo) {
    Element.prototype.scrollTo = vi.fn();
  }
});

// ----- hoisted mocks -----
const { mockSend, mockNewChat, mockStop } = vi.hoisted(() => ({
  mockSend: vi.fn(),
  mockNewChat: vi.fn(),
  mockStop: vi.fn(),
}));

// ----- hooks / modules -----
vi.mock('@/lib/LanguageProvider', () => ({
  useLanguage: () => ({ lang: 'en' }),
}));

vi.mock('../../hooks/useEciscoBiChat', () => ({
  useEciscoBiChat: vi.fn(),
}));

// Simple MessageBubble stub so we can assert message content renders
vi.mock('@/components/chat/MessageBubble', () => ({
  default: ({ message, isStreaming }) => (
    <div data-testid="msg-bubble" data-streaming={isStreaming ? 1 : 0}>
      {message.content}
    </div>
  ),
}));

import DashboardChatWidget from '../DashboardChatWidget';
import { useEciscoBiChat } from '../../hooks/useEciscoBiChat';

// Helper to render with a given hook return
function renderWithHook(defaults = {}) {
  useEciscoBiChat.mockReturnValue({
    messages: [],
    isStreaming: false,
    loadStatus: 'idle',
    send: mockSend,
    newChat: mockNewChat,
    stop: mockStop,
    ...defaults,
  });
  return render(<DashboardChatWidget />);
}

describe('DashboardChatWidget', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ---- 1. Floating button ----
  it('renders the floating chatbot button', () => {
    renderWithHook();
    const btn = screen.getByRole('button', { name: 'Ecisco BI Assistant' });
    expect(btn).toBeInTheDocument();
  });

  // ---- 2. Clicking button opens popup ----
  it('opens the popup when floating button is clicked', async () => {
    renderWithHook();
    const btn = screen.getByRole('button', { name: 'Ecisco BI Assistant' });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });
  });

  // ---- 3. Close button dismisses popup ----
  it('closes popup when X button is clicked', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    // Dialog stays in DOM but becomes hidden (pointer-events-none, opacity-0)
    await waitFor(() => {
      const dialog = screen.getByRole('dialog');
      expect(dialog.className).toContain('pointer-events-none');
      expect(dialog.className).toContain('opacity-0');
    });
  });

  // ---- 4. Escape key closes popup ----
  it('closes popup on Escape key', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => {
      const dialog = screen.getByRole('dialog');
      expect(dialog.className).toContain('pointer-events-none');
      expect(dialog.className).toContain('opacity-0');
    });
  });

  // ---- 5. + New chat ----
  it('calls newChat when + New chat button is clicked', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('New chat'));
    expect(mockNewChat).toHaveBeenCalledTimes(1);
  });

  // ---- 6. Send triggers send function ----
  it('calls send with input text when send button is clicked', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const input = screen.getByPlaceholderText('Ask me anything about markets, pricing, or reports…');
    fireEvent.change(input, { target: { value: 'Hello world' } });
    fireEvent.click(screen.getByTitle('Send'));

    expect(mockSend).toHaveBeenCalledWith('Hello world');
  });

  // ---- 7. Enter key sends ----
  it('calls send on Enter key', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const input = screen.getByPlaceholderText('Ask me anything about markets, pricing, or reports…');
    fireEvent.change(input, { target: { value: 'Test message' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: false });

    expect(mockSend).toHaveBeenCalledWith('Test message');
  });

  // ---- 8. Stop button during streaming ----
  it('shows Stop button and calls stop when streaming', async () => {
    renderWithHook({ isStreaming: true });
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const stopBtn = screen.getByTitle('Stop');
    expect(stopBtn).toBeInTheDocument();
    fireEvent.click(stopBtn);
    expect(mockStop).toHaveBeenCalledTimes(1);
  });

  // ---- 9. Input disabled when streaming ----
  it('disables input field while streaming', async () => {
    renderWithHook({ isStreaming: true });
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const input = screen.getByPlaceholderText('Ask me anything about markets, pricing, or reports…');
    expect(input).toBeDisabled();
  });

  // ---- 10. Empty state with greeting + suggestions ----
  it('shows greeting and suggestions when no messages', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    expect(screen.getByText(/I'm the Ecisco BI Assistant/)).toBeInTheDocument();
    expect(screen.getByText("Give me this week's market summary")).toBeInTheDocument();
    expect(screen.getByText("What's the price trend for crude oil?")).toBeInTheDocument();
    expect(screen.getByText('Show me the latest intelligence events')).toBeInTheDocument();
  });

  // ---- 11. Clicking a suggestion triggers send ----
  it('calls send when a suggestion chip is clicked', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    fireEvent.click(screen.getByText("Give me this week's market summary"));
    expect(mockSend).toHaveBeenCalledWith("Give me this week's market summary");
  });

  // ---- 12. Messages render via MessageBubble ----
  it('renders messages in the list', async () => {
    const sampleMessages = [
      { id: 'u1', role: 'user', content: 'Hello' },
      { id: 'a1', role: 'assistant', content: 'Hi there!', isStreaming: false },
    ];
    renderWithHook({ messages: sampleMessages });
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    // User message renders in a bubble
    expect(screen.getByText('Hello')).toBeInTheDocument();
    // Assistant message renders via MessageBubble stub
    expect(screen.getByText('Hi there!')).toBeInTheDocument();
    expect(screen.getAllByTestId('msg-bubble')).toHaveLength(1);
  });

  // ---- 13. Loading state shows spinner dots ----
  it('shows loading dots when loadStatus is loading', async () => {
    renderWithHook({ loadStatus: 'loading', messages: [] });
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    // The three animated dots should be present
    const dots = document.querySelectorAll('.animate-bounce');
    expect(dots.length).toBe(3);
  });

  // ============ Resize ============

  // ---- 14. Resize handles present ----
  it('shows resize handles when dialog is open', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    expect(screen.getByTestId('resize-handle-left')).toBeInTheDocument();
    expect(screen.getByTestId('resize-handle-top')).toBeInTheDocument();
    expect(screen.getByTestId('resize-handle-corner')).toBeInTheDocument();
  });

  // ---- 15. Left-edge drag changes width ----
  it('increases width when left edge is dragged leftward', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const leftHandle = screen.getByTestId('resize-handle-left');
    const dialog = screen.getByRole('dialog');

    fireEvent.pointerDown(leftHandle, { clientX: 600, clientY: 300 });
    // drag 70px left → width = 380 + 70 = 450
    fireEvent.pointerMove(window, { clientX: 530, clientY: 300 });

    expect(dialog.style.width).toBe('450px');
    fireEvent.pointerUp(window);
  });

  // ---- 16. Top-edge drag changes height ----
  it('decreases height when top edge is dragged downward', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const topHandle = screen.getByTestId('resize-handle-top');
    const dialog = screen.getByRole('dialog');

    fireEvent.pointerDown(topHandle, { clientX: 400, clientY: 200 });
    // drag 50px down → height = 560 + (200 - 250) = 560 - 50 = 510
    fireEvent.pointerMove(window, { clientX: 400, clientY: 250 });

    expect(dialog.style.height).toBe('510px');
    fireEvent.pointerUp(window);
  });

  // ---- 17. Corner drag changes both ----
  it('changes both width and height when corner is dragged', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const cornerHandle = screen.getByTestId('resize-handle-corner');
    const dialog = screen.getByRole('dialog');

    fireEvent.pointerDown(cornerHandle, { clientX: 600, clientY: 200 });
    // drag 80px left, 40px up → width +80, height +40
    fireEvent.pointerMove(window, { clientX: 520, clientY: 160 });

    expect(dialog.style.width).toBe('460px');
    expect(dialog.style.height).toBe('600px');
    fireEvent.pointerUp(window);
  });

  // ---- 18. Width clamped to minimum 300px ----
  it('clamps width to minimum 300px when dragged too narrow', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const leftHandle = screen.getByTestId('resize-handle-left');
    const dialog = screen.getByRole('dialog');

    fireEvent.pointerDown(leftHandle, { clientX: 600, clientY: 300 });
    // drag 500px right → 380 - 500 = -120 → clamped to 300
    fireEvent.pointerMove(window, { clientX: 1100, clientY: 300 });

    expect(dialog.style.width).toBe('300px');
    fireEvent.pointerUp(window);
  });

  // ---- 19. Height clamped to minimum 400px ----
  it('clamps height to minimum 400px when dragged below minimum', async () => {
    renderWithHook();
    fireEvent.click(screen.getByRole('button', { name: 'Ecisco BI Assistant' }));
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

    const topHandle = screen.getByTestId('resize-handle-top');
    const dialog = screen.getByRole('dialog');

    fireEvent.pointerDown(topHandle, { clientX: 400, clientY: 200 });
    // drag down far enough → 560 - large delta → clamped to 400
    fireEvent.pointerMove(window, { clientX: 400, clientY: 600 });

    expect(dialog.style.height).toBe('400px');
    fireEvent.pointerUp(window);
  });
});
