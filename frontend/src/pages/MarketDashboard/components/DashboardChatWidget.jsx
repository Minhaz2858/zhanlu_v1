import { useState, useRef, useEffect, useCallback } from 'react';
import { Bot, Plus, X, Square, Send } from 'lucide-react';
import { translations } from '@/lib/translations';
import { useLanguage } from '@/lib/LanguageProvider';
import { useEciscoBiChat } from '../hooks/useEciscoBiChat';
import MessageBubble from '@/components/chat/MessageBubble';

export default function DashboardChatWidget() {
  const { lang } = useLanguage();
  const t = translations[lang]?.marketDashboardChat || translations.en.marketDashboardChat;

  const [open, setOpen] = useState(false);
  const [input, setInput] = useState('');

  const { messages, isStreaming, loadStatus, send, newChat, stop } = useEciscoBiChat();
  const listRef = useRef(null);
  const inputRef = useRef(null);

  // ---- Resize state ----
  const MIN_SIZE = { width: 300, height: 400 };
  const DEFAULT_SIZE = { width: 380, height: 560 };
  const [size, setSize] = useState(DEFAULT_SIZE);
  const [isResizing, setIsResizing] = useState(false);
  const dragRef = useRef(null);

  const hasMessages = messages.length > 0;
  const isLoading = loadStatus === 'loading';
  const showEmpty = open && !hasMessages && !isStreaming && !isLoading;

  // Auto-scroll to bottom on new messages / deltas
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages]);

  // Focus input when popup opens
  useEffect(() => {
    if (open && inputRef.current) {
      setTimeout(() => inputRef.current?.focus(), 150);
    }
  }, [open]);

  // Escape key closes popup
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  // ---- Send handler ----
  const handleSend = useCallback(() => {
    const text = input.trim();
    if (!text || isStreaming) return;
    send(text);
    setInput('');
  }, [input, isStreaming, send]);

  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  // ---- New chat ----
  const handleNewChat = useCallback(() => {
    newChat();
    setInput('');
  }, [newChat]);

  // ---- Suggestion chip click ----
  const handleSuggestion = useCallback(
    (sug) => {
      send(sug);
      setInput('');
    },
    [send],
  );

  // ---- Resize handlers ----
  const clampSize = useCallback((w, h) => {
    const maxW = Math.floor(window.innerWidth * 0.9);
    const maxH = Math.floor(window.innerHeight * 0.9);
    return {
      width: Math.round(Math.min(Math.max(w, MIN_SIZE.width), maxW)),
      height: Math.round(Math.min(Math.max(h, MIN_SIZE.height), maxH)),
    };
  }, []);

  const startResize = useCallback((dir) => (e) => {
    e.preventDefault();
    setIsResizing(true);
    dragRef.current = {
      dir,
      startX: e.clientX,
      startY: e.clientY,
      startW: size.width,
      startH: size.height,
    };
  }, [size]);

  useEffect(() => {
    if (!isResizing) return;

    const onMove = (e) => {
      const d = dragRef.current;
      if (!d) return;
      let w = d.startW;
      let h = d.startH;

      if (d.dir === 'left' || d.dir === 'corner') {
        w = d.startW + (d.startX - e.clientX);
      }
      if (d.dir === 'top' || d.dir === 'corner') {
        h = d.startH + (d.startY - e.clientY);
      }

      const clamped = clampSize(w, h);
      setSize(clamped);
    };

    const onUp = () => {
      setIsResizing(false);
      dragRef.current = null;
    };

    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
  }, [isResizing, clampSize]);

  return (
    <>
      {/* ======== Floating Button ======== */}
      <button
        onClick={() => setOpen((o) => !o)}
        className={`
          fixed bottom-6 right-6 z-50
          flex items-center justify-center
          w-14 h-14 rounded-full
          bg-gradient-to-br from-indigo-500 to-violet-600
          ring-2 ring-white/20
          shadow-xl shadow-indigo-500/25
          text-white
          transition-all duration-300 ease-out
          hover:scale-110 hover:shadow-2xl hover:shadow-indigo-500/30 hover:ring-white/40
          active:scale-95
          ${open ? 'opacity-0 pointer-events-none scale-75' : 'opacity-100'}
        `}
        aria-label={t.title}
      >
        <Bot className="w-6 h-6" />
      </button>

      {/* ======== Chat Popup ======== */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t.title}
        style={{ width: size.width, height: size.height }}
        className={`
          fixed bottom-24 right-6 z-50
          flex flex-col
          bg-white/85 dark:bg-slate-900/85
          backdrop-blur-xl
          border border-white/20 dark:border-slate-700/50
          rounded-2xl
          shadow-2xl shadow-black/10 dark:shadow-black/30
          origin-bottom-right
          ${!isResizing ? 'transition-all duration-200 ease-out' : ''}
          ${open
            ? 'opacity-100 scale-100 translate-y-0 pointer-events-auto'
            : 'opacity-0 scale-95 translate-y-2 pointer-events-none'
          }
        `}
      >
        {/* ---- Header ---- */}
        <div className="relative z-10 flex items-center justify-between h-14 px-4 border-b border-white/10 dark:border-slate-700/30 shrink-0">
          <div className="flex items-center gap-2.5">
            <div className="flex items-center justify-center w-8 h-8 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 text-white">
              <Bot className="w-4 h-4" />
            </div>
            <span className="font-semibold text-sm text-slate-800 dark:text-white">
              {t.title}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={handleNewChat}
              className="flex items-center gap-1 px-2.5 py-1.5 text-xs text-slate-500 dark:text-slate-400 hover:bg-white/10 dark:hover:bg-slate-700/50 rounded-lg transition-colors"
              title={t.newChat}
            >
              <Plus className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t.newChat}</span>
            </button>
            <button
              onClick={() => setOpen(false)}
              className="flex items-center justify-center w-8 h-8 rounded-lg text-slate-500 dark:text-slate-400 hover:bg-red-500/10 hover:text-red-500 transition-colors"
              aria-label={t.close}
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* ---- Message List ---- */}
        <div
          ref={listRef}
          className="flex-1 overflow-y-auto px-3 py-3 space-y-3"
        >
          {isLoading && (
            <div className="flex items-center justify-center h-full">
              <div className="flex items-center gap-2 text-slate-400 dark:text-slate-500">
                <div className="w-2 h-2 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '0ms' }} />
                <div className="w-2 h-2 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '150ms' }} />
                <div className="w-2 h-2 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            </div>
          )}

          {showEmpty && (
            <div className="flex flex-col items-center justify-center h-full px-2 text-center">
              <div className="flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-600 text-white mb-4 shadow-lg shadow-indigo-500/20">
                <Bot className="w-8 h-8" />
              </div>
              <p className="text-sm text-slate-600 dark:text-slate-300 leading-relaxed max-w-xs mb-4">
                {t.emptyGreeting}
              </p>
              <div className="flex flex-col gap-2 w-full max-w-[260px]">
                {Array.isArray(t.suggestions) && t.suggestions.map((sug, i) => (
                  <button
                    key={i}
                    onClick={() => handleSuggestion(sug)}
                    className="text-xs text-left text-slate-500 dark:text-slate-400 bg-white/50 dark:bg-slate-800/50 border border-slate-200/50 dark:border-slate-700/30 rounded-xl px-3 py-2.5 hover:bg-indigo-50 dark:hover:bg-indigo-900/20 hover:text-indigo-600 dark:hover:text-indigo-300 hover:border-indigo-200 dark:hover:border-indigo-700/50 transition-all"
                  >
                    {sug}
                  </button>
                ))}
              </div>
            </div>
          )}

          {!showEmpty && !isLoading && messages.map((msg) => (
            <div key={msg.id} className={msg.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
              {msg.role === 'user' ? (
                <div className="max-w-[85%] px-3.5 py-2.5 rounded-2xl rounded-br-md bg-indigo-500 text-white text-sm shadow-sm">
                  {msg.content}
                </div>
              ) : (
                <div className="max-w-[90%]">
                  <MessageBubble
                    message={msg}
                    isStreaming={msg.isStreaming || false}
                  />
                </div>
              )}
            </div>
          ))}
        </div>

        {/* ---- Resize handles ---- */}
        <div
          data-testid="resize-handle-left"
          aria-hidden="true"
          onPointerDown={startResize('left')}
          className="absolute left-0 top-0 bottom-0 w-2 cursor-ew-resize z-0 hover:bg-indigo-500/10"
          style={{ touchAction: 'none' }}
        />
        <div
          data-testid="resize-handle-top"
          aria-hidden="true"
          onPointerDown={startResize('top')}
          className="absolute top-0 left-0 right-0 h-2 cursor-ns-resize z-0 hover:bg-indigo-500/10"
          style={{ touchAction: 'none' }}
        />
        <div
          data-testid="resize-handle-corner"
          aria-hidden="true"
          onPointerDown={startResize('corner')}
          className="absolute top-0 left-0 w-6 h-6 cursor-nwse-resize z-0 hover:bg-indigo-500/10 rounded-tl-2xl"
          style={{ touchAction: 'none' }}
        />

        {/* ---- Input Area ---- */}
        <div className="p-3 border-t border-white/10 dark:border-slate-700/30 shrink-0">
          <div className="flex items-center gap-2">
            <div className="flex-1 flex items-center bg-white/60 dark:bg-slate-800/60 backdrop-blur border border-slate-200/50 dark:border-slate-700/30 rounded-full px-4 py-2">
              <input
                ref={inputRef}
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={t.placeholder}
                disabled={isStreaming}
                className="flex-1 bg-transparent border-none outline-none text-sm text-slate-800 dark:text-white placeholder-slate-400 dark:placeholder-slate-500"
              />
            </div>

            {isStreaming ? (
              <button
                onClick={stop}
                className="flex items-center justify-center w-9 h-9 rounded-full bg-zinc-100 hover:bg-zinc-200 border border-zinc-200/80 text-zinc-600 transition-all active:scale-95"
                title={t.stop}
              >
                <Square className="w-3.5 h-3.5 fill-current" />
              </button>
            ) : (
              <button
                onClick={handleSend}
                disabled={!input.trim()}
                className="flex items-center justify-center w-9 h-9 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 hover:from-indigo-600 hover:to-violet-700 text-white shadow-lg shadow-indigo-500/25 transition-all active:scale-95 disabled:opacity-40 disabled:shadow-none disabled:cursor-not-allowed"
                title={lang === 'zh' ? '发送' : 'Send'}
              >
                <Send className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
