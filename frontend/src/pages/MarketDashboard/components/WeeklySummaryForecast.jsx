import { useState, useEffect } from 'react';
import { weeklyDigestApi } from '../../../api/marketDashboard';
import ReportList from './WeeklyDigest/ReportList';
import ReportDetail from './WeeklyDigest/ReportDetail';

export default function WeeklySummaryForecast() {
  const [selectedId, setSelectedId] = useState(null);
  // Default on; flipped off only when the backend reports the feature is
  // disabled (GET /latest returns 404 with "not enabled").
  const [featureEnabled, setFeatureEnabled] = useState(true);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        await weeklyDigestApi.latest();
        if (mounted) setFeatureEnabled(true);
      } catch (e) {
        if (mounted && /not enabled/i.test(e?.message || '')) {
          setFeatureEnabled(false);
        }
      }
    })();
    return () => { mounted = false; };
  }, []);

  return (
    <div className="flex h-full bg-white rounded-lg border border-slate-200 overflow-hidden">
      <ReportList
        selectedId={selectedId}
        onSelect={setSelectedId}
        featureEnabled={featureEnabled}
      />
      <ReportDetail reportId={selectedId} />
    </div>
  );
}
