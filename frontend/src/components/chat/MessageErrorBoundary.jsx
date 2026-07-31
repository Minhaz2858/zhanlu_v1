/**
 * MessageErrorBoundary — defense-in-depth for the chat message list.
 *
 * A render-time crash in any message component (e.g. a hooks-order
 * violation like the ActivitySteps bug, or a malformed [[RESULT]] block)
 * used to unmount the ENTIRE React root, leaving the user staring at a
 * blank white screen that only a manual refresh could recover from.
 *
 * Wrapping the message list in this boundary contains the crash to the
 * list itself: the rest of the app (sidebar, input, header) keeps working
 * and the user sees an inline error with a one-click retry instead of a
 * dead page.
 */
import React from 'react';

export default class MessageErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Surface for observability; do not swallow silently.
    console.error('[MessageErrorBoundary] message render crashed:', error, info?.componentStack);
  }

  handleReset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return (
        <div className="mx-auto my-6 max-w-md rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-center">
          <p className="text-sm font-medium text-destructive">
            Something went wrong while rendering the conversation.
          </p>
          <p className="mt-1 break-words text-xs text-muted-foreground">
            {String(this.state.error?.message || this.state.error)}
          </p>
          <button
            onClick={this.handleReset}
            className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
