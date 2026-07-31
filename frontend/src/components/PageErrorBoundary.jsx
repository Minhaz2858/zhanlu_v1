import React from 'react';
import { Loader2, RotateCcw, AlertTriangle } from 'lucide-react';

/**
 * PageErrorBoundary — minimal class error boundary to keep the rest of
 * the SPA reachable when a single page throws during render.
 *
 * Today this protects the user from a single bad `.map()` / `.filter()`
 * call leaving them staring at a permanently blank screen (e.g. legacy
 * rows with capabilities stored as a bare string). Without an
 * ErrorBoundary, a thrown render error inside AgentConfig unmounts the
 * entire `<Outlet />` and the user has to manually clear the URL or
 * bounce to /login to recover — easy to miss in QA.
 *
 * Lives at the AppLayout level so every route is covered without
 * each page author having to remember to wrap their tree.
 */
export default class PageErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Keep the console breadcrumb so developers can find the stack.
    // eslint-disable-next-line no-console
    console.error('[PageErrorBoundary] page render crashed:', error, info?.componentStack);
  }

  handleReload = () => {
    this.setState({ error: null });
    if (typeof window !== 'undefined') window.location.reload();
  };

  handleRetry = () => {
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="flex h-full w-full items-center justify-center bg-background px-6">
        <div className="max-w-md rounded-2xl border border-border bg-card p-8 text-center shadow-sm">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10 text-destructive">
            <AlertTriangle className="h-6 w-6" />
          </div>
          <h2 className="font-display text-lg font-semibold text-foreground">
            页面出了点小问题
          </h2>
          <p className="mt-2 text-xs text-muted-foreground">
            该页面渲染时遇到了异常。已记录错误信息，请尝试刷新或返回上级页面。
          </p>
          {error?.message && (
            <pre className="mt-3 max-h-32 overflow-auto rounded-md border border-border bg-muted px-3 py-2 text-left text-[11px] text-muted-foreground">
{String(error.message)}
            </pre>
          )}
          <div className="mt-5 flex justify-center gap-2">
            <button
              type="button"
              onClick={this.handleRetry}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground hover:bg-secondary"
            >
              <Loader2 className="h-3 w-3" />
              重试
            </button>
            <button
              type="button"
              onClick={this.handleReload}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
            >
              <RotateCcw className="h-3 w-3" />
              刷新整页
            </button>
          </div>
        </div>
      </div>
    );
  }
}
