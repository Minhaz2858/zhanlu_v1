import { useState } from 'react';
import ReportList from './WeeklyDigest/ReportList';
import ReportDetail from './WeeklyDigest/ReportDetail';

export default function WeeklySummaryForecast() {
  const [selectedId, setSelectedId] = useState(null);

  return (
    <div className="flex h-full bg-white rounded-lg border border-slate-200 overflow-hidden">
      <ReportList selectedId={selectedId} onSelect={setSelectedId} />
      <ReportDetail reportId={selectedId} />
    </div>
  );
}
