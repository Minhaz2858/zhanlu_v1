export default function WidgetShell({ children, title, loading, error, empty }) {
  return (
    <section className="min-h-[132px] rounded-lg border border-border bg-card shadow-sm">
      <div className="flex items-start justify-between gap-3 border-b border-border/70 px-4 py-3">
        <h3 className="truncate text-sm font-semibold text-foreground">{title}</h3>
      </div>
      <div className="p-4">
        {loading ? (
          <div data-testid="widget-skeleton" className="space-y-3">
            <div className="h-8 w-24 animate-pulse rounded bg-secondary" />
            <div className="h-3 w-2/3 animate-pulse rounded bg-secondary" />
          </div>
        ) : error ? (
          <div className="rounded-md border border-destructive/20 bg-destructive/5 p-3 text-sm text-destructive">
            {error}
          </div>
        ) : empty ? (
          <div className="flex min-h-[72px] items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
            No data yet
          </div>
        ) : children}
      </div>
    </section>
  );
}