import { useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from './lib/typography';

const VALID_SECTIONS = ['overview', 'upstream', 'midstream', 'downstream'];

export default function MarketDashboardPage() {
  const { section } = useParams();

  if (!VALID_SECTIONS.includes(section)) {
    return (
      <div className="p-8">
        <h1 className={TYPO.pageTitle}>Unknown section: {section}</h1>
        <p className={TYPO.listText + ' mt-2'}>
          Valid sections: {VALID_SECTIONS.join(', ')}
        </p>
      </div>
    );
  }

  const enabledQuery = useQuery({
    queryKey: ['market-dashboard', 'enabled'],
    queryFn: marketDashboardApi.enabled,
    staleTime: 30_000,
  });

  if (enabledQuery.isLoading) {
    return <div className="p-8 text-sm text-slate-500">Checking EDIA availability…</div>;
  }

  if (enabledQuery.isError || !enabledQuery.data?.enabled) {
    return (
      <div className="p-8">
        <h1 className={TYPO.pageTitle}>EDIA Market Dashboard — {section}</h1>
        <div className="mt-4 rounded border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          EDIA data source is not configured on this server. Set
          <code className="mx-1 rounded bg-amber-100 px-1">EDIA_MYSQL_URL</code>
          in the backend environment to enable the dashboard.
        </div>
      </div>
    );
  }

  if (enabledQuery.data && !enabledQuery.data.reachable) {
    return (
      <div className="p-8">
        <h1 className={TYPO.pageTitle}>EDIA Market Dashboard — {section}</h1>
        <div className="mt-4 rounded border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          EDIA MySQL warehouse is configured but currently unreachable.
          Reason: {enabledQuery.data.reason || 'configured_unreachable'}
        </div>
      </div>
    );
  }

  return (
    <div className="p-8">
      <h1 className={TYPO.pageTitle}>EDIA Market Dashboard — {section}</h1>
      <p className={TYPO.listText + ' mt-1 text-slate-500'}>
        Snapshot + chart components will be wired in Phase C/D/E.
      </p>
    </div>
  );
}
