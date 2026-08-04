import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from './lib/typography';
import RegionalTable from './components/RegionalTable';
import OverviewDashboard from './components/OverviewDashboard';
import ObservedDataStrip from './components/ObservedDataStrip';
import ForecastTrendChart from './components/ForecastTrendChart';
import PriceTransmissionChain from './components/PriceTransmissionChain';
import DecisionBoard from './components/DecisionBoard';

const VALID_SECTIONS = ['overview', 'upstream', 'midstream', 'downstream'];

// Upstream fallback so the strip paints instantly while the snapshot loads.
// (Midstream products like Isoprene/Piperylene/DCPD and downstream products
//  appear as targets in the Price Transmission Chain visualization below.)
const UPSTREAM_PRODUCTS = ['crude_oil', 'naphtha', 'cracked_c5'];

const PRODUCT_LABELS = {
  crude_oil: { en: 'Brent Crude Oil', zh: '布伦特原油' },
  naphtha: { en: 'Japanese Naphtha', zh: '日本石脑油' },
  cracked_c5: { en: 'Cracked C5', zh: '裂解碳五' },
  cracked_c9: { en: 'Cracked C9', zh: '裂解碳九' },
  isoprene: { en: 'Isoprene', zh: '异戊二烯' },
  piperylene: { en: 'Piperylene', zh: '间戊二烯' },
  dcpd: { en: 'DCPD', zh: '双环戊二烯' },
};

export default function MarketDashboardPage() {
  const { section } = useParams();
  const isValidSection = VALID_SECTIONS.includes(section);
  const isProductSection = isValidSection && section !== 'overview';
  const [selectedProductId, setSelectedProductId] = useState('crude_oil');

  // All hooks MUST be called before any conditional return (React Rules of Hooks).
  const enabledQuery = useQuery({
    queryKey: ['market-dashboard', 'enabled'],
    queryFn: marketDashboardApi.enabled,
    staleTime: 30_000,
  });

  const snapshotQuery = useQuery({
    queryKey: ['market-dashboard', 'snapshot', section],
    queryFn: () => marketDashboardApi.snapshot(
      section === 'overview' ? undefined : section,
    ),
    enabled: isValidSection && enabledQuery.data?.enabled && enabledQuery.data?.reachable,
    staleTime: 60_000,
  });

  const snapshotItems = useMemo(
    () => (Array.isArray(snapshotQuery.data) ? snapshotQuery.data : []),
    [snapshotQuery.data],
  );

  // Product list comes from the snapshot (stays in sync with backend config).
  // Upstream falls back to the static 3-product list for instant first paint.
  const productIds = useMemo(() => {
    if (snapshotItems.length) return snapshotItems.map((s) => s.id);
    return section === 'upstream' ? UPSTREAM_PRODUCTS : [];
  }, [snapshotItems, section]);

  // Strip labels: snapshot names first, static labels as fallback.
  const stripLabels = useMemo(() => {
    const map = { ...PRODUCT_LABELS };
    for (const s of snapshotItems) {
      if (s?.id && s?.name) map[s.id] = { en: s.name, zh: s.name };
    }
    return map;
  }, [snapshotItems]);

  // Keep the selection valid per section: reset to the section's first
  // product when the current selection isn't part of it (e.g. navigating
  // upstream → midstream).
  useEffect(() => {
    if (!isProductSection || !productIds.length) return;
    if (!productIds.includes(selectedProductId)) {
      setSelectedProductId(productIds[0]);
    }
  }, [isProductSection, productIds, selectedProductId]);

  const productName = useMemo(() => {
    const label = stripLabels[selectedProductId];
    return label ? label.en : selectedProductId;
  }, [stripLabels, selectedProductId]);

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

  // Overview section: pixel-faithful EDIA layout (sidebar + 3 price cards +
  // 7-day forecast + industry intelligence + footer).
  if (section === 'overview') {
    return <OverviewDashboard snapshot={snapshotQuery.data || []} />;
  }

  // Unified product-section layout (upstream / midstream / downstream):
  // observed-data strip (product selector) → EDIA overlay forecast chart →
  // regional table for the selected product.
  //
  // AppLayout's <main> uses overflow-hidden + h-full, so the page itself
  // must own the vertical scroll via h-full overflow-y-auto — otherwise the
  // bottom of the page (price chain tail, regional table) is clipped and
  // unreachable. OverviewDashboard applies the same pattern on its root.
  return (
    <div className="flex h-full flex-col gap-0 overflow-y-auto">
      <ObservedDataStrip
        products={productIds}
        labels={stripLabels}
        selectedId={selectedProductId}
        onSelect={setSelectedProductId}
      />
      <DecisionBoard
        group={section}
        selectedId={selectedProductId}
        onSelectProduct={setSelectedProductId}
      />
      <DecisionBoard
        group={section}
        selectedId={selectedProductId}
        onSelectProduct={setSelectedProductId}
      />
      {snapshotQuery.isLoading && (
        <div className="px-6 py-4">
          <p className={TYPO.listText + ' text-slate-500'}>Loading snapshot…</p>
        </div>
      )}
      {snapshotQuery.isError && (
        <div className="mx-6 mt-4 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          Failed to load snapshot: {snapshotQuery.error?.message || 'unknown'}
        </div>
      )}
      {!snapshotQuery.isLoading && !productIds.length && (
        <div className="px-6 py-4">
          <p className={TYPO.listText + ' text-slate-500'}>
            No products configured for this section.
          </p>
        </div>
      )}
      {selectedProductId && productIds.includes(selectedProductId) && (
        <ForecastTrendChart
          productId={selectedProductId}
          productName={productName}
        />
      )}
      {section === 'upstream' && <PriceTransmissionChain />}
      {selectedProductId && productIds.includes(selectedProductId) && (
        <div className="px-6 pt-6 pb-8">
          <RegionalTable productId={selectedProductId} />
        </div>
      )}
    </div>
  );
}
