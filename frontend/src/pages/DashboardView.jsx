import { useEffect, useState } from 'react';
import { useParams, useSearchParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Loader2 } from 'lucide-react';
import { getDashboard, getDashboardApp } from '@/api/dashboards';
import DashboardViewer from '@/components/dashboard/DashboardViewer';
import FullStackDashboardViewer from '@/components/dashboard/FullStackDashboardViewer';

function parseRange(search) {
  const from = search.get('from');
  const to = search.get('to');
  if (from || to) return { from: from || undefined, to: to || undefined };
  return undefined; // let the viewer default to last-30-days
}

function parseFilters(search) {
  const raw = search.get('filters');
  if (!raw) return undefined;
  try {
    const obj = JSON.parse(raw);
    return obj && typeof obj === 'object' ? obj : undefined;
  } catch {
    return undefined;
  }
}

export default function DashboardView() {
  const { id } = useParams();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const [dashboard, setDashboard] = useState(null);
  const [appRecord, setAppRecord] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setDashboard(null);
    setAppRecord(null);

    // Try the full-stack app record first. Legacy dashboards are stored in the
    // `dashboards` table (getDashboard), new ones in `dashboard_apps`
    // (getDashboardApp). The app record 404s for legacy ids; fall back to the
    // legacy fetch on any failure.
    getDashboardApp(id)
      .then((app) => { if (alive) { setAppRecord(app); setLoading(false); } })
      .catch(() => {
        if (!alive) return;
        getDashboard(id)
          .then((d) => { if (alive) { setDashboard(d); setLoading(false); } })
          .catch((e) => { if (alive) { setError(String(e)); setLoading(false); } });
      });
    return () => { alive = false; };
  }, [id]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <button
          onClick={() => navigate(-1)}
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" /> Back
        </button>
        <span className="truncate text-sm font-medium">
          {dashboard?.name || appRecord?.name || 'Dashboard'}
        </span>
      </div>
      <div className="min-h-0 flex-1">
        {error ? (
          <div className="p-4 text-sm text-destructive">{error}</div>
        ) : loading ? (
          <div className="flex h-full items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : appRecord ? (
          <FullStackDashboardViewer
            appUrl={appRecord.app_url || `/api/dashboards/apps/${id}/`}
            name={appRecord.name}
            slug={appRecord.slug || id}
          />
        ) : (
          <DashboardViewer
            dashboard={dashboard}
            canEdit={!!dashboard.can_edit}
            polling
            initialDateRange={parseRange(search)}
            initialFilters={parseFilters(search)}
            onSaved={setDashboard}
            onCopied={(c) => navigate(`/dashboard/${c.id}`)}
          />
        )}
      </div>
    </div>
  );
}
