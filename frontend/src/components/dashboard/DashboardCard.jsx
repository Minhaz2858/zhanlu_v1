import { LayoutDashboard } from 'lucide-react';

export default function DashboardCard({ artifact, onOpen }) {
  return (
    <div className="my-2 flex items-center justify-between rounded-xl border border-border bg-card p-3 shadow-sm transition-shadow hover:shadow-md">
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10">
          <LayoutDashboard className="h-4 w-4 text-primary" />
        </div>
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{artifact.title}</div>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-[hsl(var(--chart-2))]" />
            <span className="truncate">
              {artifact.datasource_name} · {artifact.widget_count} widgets · live
            </span>
          </div>
        </div>
      </div>
      <button
        onClick={onOpen}
        className="ml-3 shrink-0 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
      >
        Open
      </button>
    </div>
  );
}
