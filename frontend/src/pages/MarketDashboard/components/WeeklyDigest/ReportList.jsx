import { useState, useEffect, useRef } from 'react';
import { Play, Loader2 } from 'lucide-react';
import { useToast } from '@/components/ui/use-toast';
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

const POLL_INTERVAL_MS = 4000;
const POLL_TIMEOUT_MS = 5 * 60 * 1000; // stop polling after 5 min

export default function ReportList({ selectedId, onSelect, featureEnabled = true }) {
  const [reports, setReports] = useState([]);
  const [loading, setLoading] = useState(true);
  const [triggering, setTriggering] = useState(false);
  const preTriggerKeyRef = useRef('');
  const { toast } = useToast();

  async function loadList() {
    const data = await weeklyDigestApi.list(1, 50);
    return data?.items || [];
  }

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const items = await loadList();
        if (mounted) setReports(items);
      } catch (e) {
        console.error('Failed to load weekly digest list:', e);
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => { mounted = false; };
  }, []);

  // While a generation is running, poll the list every 4s until the newest
  // card settles into a terminal state (ready/failed) or the 5-min budget runs out.
  useEffect(() => {
    if (!triggering) return;
    let cancelled = false;
    const startedAt = Date.now();

    const poll = async () => {
      try {
        const items = await loadList();
        if (cancelled) return;
        setReports(items);
        const latest = items[0];
        const latestKey = latest ? `${latest.id}:${latest.status}` : '';
        const settled = latest && (latest.status === 'ready' || latest.status === 'failed');
        if (settled && latestKey !== preTriggerKeyRef.current) {
          // A new/updated card appeared and is no longer generating → done.
          setTriggering(false);
          if (latest.status === 'ready') {
            toast({ description: '周度报告生成完成 / Weekly report ready' });
          }
          return;
        }
        if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
          setTriggering(false);
          toast({ description: '生成仍在进行中，请稍后查看 / Generation may still be running' });
        }
      } catch (e) {
        console.error('Failed to refresh weekly digest list:', e);
        if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
          setTriggering(false);
        }
      }
    };

    poll();
    const timer = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [triggering, toast]);

  async function handleTrigger() {
    if (!featureEnabled || triggering) return;
    // Snapshot the current newest card so the poll can tell when generation
    // produces a new/updated terminal card (re-trigger reuses the same id).
    preTriggerKeyRef.current = reports[0] ? `${reports[0].id}:${reports[0].status}` : '';
    setTriggering(true);
    try {
      await weeklyDigestApi.trigger();
      toast({ description: '生成已启动 / Generation triggered' });
      try {
        setReports(await loadList());
      } catch (e) {
        console.error('Failed to refresh list after trigger:', e);
      }
    } catch (e) {
      console.error('Failed to trigger weekly digest:', e);
      setTriggering(false);
      toast({ variant: 'destructive', description: e?.message || '生成启动失败 / Trigger failed' });
    }
  }

  return (
    <div className="w-[280px] min-w-[280px] border-r border-slate-200 bg-slate-50 flex flex-col h-full">
      <div className="p-3 border-b border-slate-200">
        <div className="flex items-start justify-between gap-2">
          <div>
            <h2 className={TYPO.cardTitle}>周度报告</h2>
            <p className="text-xs text-slate-500 mt-0.5">C5/C9 Weekly Reports</p>
          </div>
          <button
            type="button"
            onClick={handleTrigger}
            disabled={!featureEnabled || triggering}
            title={
              !featureEnabled
                ? '每周报告生成功能未启用 / Weekly digest feature disabled'
                : '生成报告 / Run now'
            }
            className="inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1.5 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors shrink-0"
          >
            {triggering ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Play className="h-3.5 w-3.5" />
            )}
            {triggering ? '生成中…' : '生成报告'}
          </button>
        </div>
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
