import { useState, useEffect } from 'react';
import { X } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';

const fmt = (n) => (n == null || n === '' ? '—' : Number(n).toLocaleString());

export default function ForecastFeedbackModal({ productId, aiPrice, onClose }) {
  const [userPrice, setUserPrice] = useState('');
  const [reason, setReason] = useState('');
  const [err, setErr] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [history, setHistory] = useState([]);

  useEffect(() => {
    let alive = true;
    marketDashboardApi.listForecastFeedback(productId)
      .then((rows) => { if (alive) setHistory(Array.isArray(rows) ? rows : []); })
      .catch(() => {});
    return () => { alive = false; };
  }, [productId]);

  const submit = async () => {
    const v = Number(userPrice);
    if (!Number.isFinite(v) || v <= 0) { setErr('价格必须为正数'); return; }
    setErr('');
    setSubmitting(true);
    try {
      await marketDashboardApi.submitForecastFeedback({
        product_id: productId,
        ai_price: aiPrice,
        user_price: v,
        reason,
        target_date: new Date().toISOString(),
      });
      setUserPrice('');
      setReason('');
      const rows = await marketDashboardApi.listForecastFeedback(productId);
      setHistory(Array.isArray(rows) ? rows : []);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">修正预测 · {productId}</h3>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600"><X size={16} /></button>
        </div>
        <label className="mb-1 block text-xs text-slate-500">AI 预测价</label>
        <input value={aiPrice ?? ''} disabled
          className="mb-3 w-full rounded border border-slate-200 bg-slate-50 px-2 py-1 text-sm dark:bg-slate-800" />
        <label className="mb-1 block text-xs text-slate-500">我认为应该是</label>
        <input aria-label="我认为应该是" value={userPrice}
          onChange={(e) => setUserPrice(e.target.value)}
          className="mb-3 w-full rounded border border-slate-200 px-2 py-1 text-sm dark:bg-slate-800 dark:border-slate-700" />
        <label className="mb-1 block text-xs text-slate-500">理由</label>
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2}
          className="mb-3 w-full rounded border border-slate-200 px-2 py-1 text-sm dark:bg-slate-800 dark:border-slate-700" />
        {err && <p className="mb-2 text-xs text-red-600">{err}</p>}
        <button onClick={submit} disabled={submitting}
          className="w-full rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-50">
          {submitting ? '提交中…' : '提交'}
        </button>

        {history.length > 0 && (
          <div className="mt-4 border-t border-slate-100 pt-2 dark:border-slate-800">
            <p className="mb-1 text-xs font-medium text-slate-500">历史修正</p>
            {history.map((h) => (
              <div key={h.id} className="flex justify-between py-0.5 text-xs text-slate-600 dark:text-slate-300">
                <span>AI {fmt(h.ai_price)} → {fmt(h.user_price)}</span>
                <span className={h.beat ? 'text-emerald-600' : 'text-slate-400'}>
                  {h.status === 'scored' ? (h.beat ? '命中' : '未中') : '待评'}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
