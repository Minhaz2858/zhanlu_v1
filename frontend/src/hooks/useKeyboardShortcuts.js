import { useEffect } from 'react';

/**
 * useKeyboardShortcuts — Global keyboard shortcuts for the chat.
 *
 * - Escape: Stop generating / close panels
 * - ArrowUp (when focus is on the chat textarea and it's empty): Edit last user message
 *
 * `active` is true when shortcuts should fire (e.g., when no modal/overlay is open).
 *
 * Note: Enter-to-send and Shift+Enter-for-newline are handled natively
 * by the ChatInput textarea's onKeyDown handler, not here.
 */
export default function useKeyboardShortcuts({
  onStop,
  onEditLast,
  active = true,
}) {
  useEffect(() => {
    if (!active) return;

    function handleKeyDown(e) {
      const tag = (e.target?.tagName || '').toLowerCase();

      // Escape: Stop generating (works globally, even inside textarea)
      if (e.key === 'Escape') {
        e.preventDefault();
        onStop?.();
        return;
      }

      // ArrowUp in empty chat textarea: Edit last user message
      if (e.key === 'ArrowUp' && tag === 'textarea' && !e.target?.value?.trim()) {
        e.preventDefault();
        onEditLast?.();
        return;
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [active, onStop, onEditLast]);
}
