import { useState, useEffect } from 'react';
import { weeklyDigestApi } from '../../../../api/marketDashboard';
import { TYPO } from '../../lib/typography';

const STATUS_BADGE = {
  ready: 'bg-green-100 text-green-700',
  generating: 'bg-yellow-100 text-yellow-700',
  failed: 'bg-red-100 text-red-700',
};

const STATUS_DOT = {
  ready: 'bg-green-500',
  generating: 'bg-yellow-500 animate-pulse',
  failed: 'bg-red-500',
};

export default function ReportList({ selectedId, onSelect }) {
  const [reports, setReports] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const data = await weeklyDigestApi.list(1, 50);
        if (mounted && data?.items) setReports(data.items);
      } catch (e) {
        console.error('Failed to load weekly digest list:', e);
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => { mounted = false; };
  }, []);

  return (
    <div className="w-[280px] min-w-[280px] border-r border-slate-200 bg-slate-50 flex flex-col h-full">
      <div className="p-3 border-b border-slate-200">
        <h2 className={TYPO.cardTitle}>周度报告</h2>
        <p className="text-xs text-slate-500 mt-0.5">C5/C9 Weekly Reports</p>
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading && (
          <div className="p-4 text-center text-sm text-slate-400">Loading...</div>
        )}

        {!loading && reports.length === 0 && (
          <div className="p-4 text-center text-sm text-slate-400">
            暂无周度报告
            <br />
            <span className="text-xs">No reports yet</span>
          </div>
        )}

        {reports.map((r) => (
          <button
            key={r.id}
            onClick={() => onSelect(r.id)}
            className={`w-full text-left px-3 py-2.5 border-b border-slate-100 transition-colors cursor-pointer
              ${selectedId === r.id ? 'bg-blue-50 border-l-2 border-l-blue-500' : 'hover:bg-slate-100 border-l-2 border-l-transparent'}`}
          >
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-slate-800 truncate">
                {r.title_zh || r.title_en}
              </span>
              <span className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full ${STATUS_BADGE[r.status] || ''}`}>
                <span className={`w-1.5 h-1.5 rounded-full ${STATUS_DOT[r.status] || ''}`} />
                {r.status}
              </span>
            </div>
            <div className="text-xs text-slate-500 mt-0.5">
              {r.week_start} ~ {r.week_end}
              <span className="ml-2">{r.product_count} products</span>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
