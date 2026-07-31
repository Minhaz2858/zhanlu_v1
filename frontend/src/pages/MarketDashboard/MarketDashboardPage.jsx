import { useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from './lib/typography';
import SnapshotGrid from './components/SnapshotGrid';
import PriceChart from './components/PriceChart';

const VALID_SECTIONS = ['overview', 'upstream', 'midstream', 'downstream'];

export default function MarketDashboardPage() {
  const { section } = useParams();
  const isValidSection = VALID_SECTIONS.includes(section);

  // All hooks MUST be called before any conditional return (React Rules of Hooks).
  const enabledQuery = useQuery({
    queryKey: ['market-dashboard', 'enabled'],
    queryFn: marketDashboardApi.enabled,
    staleTime: 30_000,
  });

  const snapshotQuery = useQuery({
    queryKey: ['market-dashboard', 'snapshot', section],
    queryFn: () => marketDashboardApi.snapshot(
      section === 'overview' ? undefined : section
    ),
    enabled: isValidSection && enabledQuery.data?.enabled && enabledQuery.data?.reachable,
    staleTime: 60_000,
  });

  if (!isValidSection) {
    return (
      <div className="p-8">
        <h1 className={TYPO.pageTitle}>Unknown section: {section}</h1>
        <p className={TYPO.listText + ' mt-2'}>
          Valid sections: {VALID_SECTIONS.join(', ')}
        </p>
      </div>
    );
  }

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
      <div className="mt-4">
        {snapshotQuery.isLoading && (
          <p className={TYPO.listText + ' text-slate-500'}>Loading snapshot…</p>
        )}
        {snapshotQuery.isError && (
          <div className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            Failed to load snapshot: {snapshotQuery.error?.message || 'unknown'}
          </div>
        )}
        {snapshotQuery.data && <SnapshotGrid items={snapshotQuery.data} />}
        {snapshotQuery.data && snapshotQuery.data.length > 0 && (
          <div className="mt-6 grid grid-cols-1 gap-3 lg:grid-cols-2">
            {snapshotQuery.data.map((s) => (
              <PriceChart key={s.id} productId={s.id} days={30} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
