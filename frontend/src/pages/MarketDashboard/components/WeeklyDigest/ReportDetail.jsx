import { useState, useEffect, useMemo } from 'react';
import { FileText, AlertTriangle, TrendingUp, TrendingDown, Calendar, BarChart3, Globe2, ListChecks, FileDown, Loader2 } from 'lucide-react';
import { useToast } from '@/components/ui/use-toast';
import { weeklyDigestApi } from '../../../../api/marketDashboard';
import ProductCard, { MarkdownText } from './ProductCard';
import { TYPO } from '../../lib/typography';

const T = {
  exportPptx: 'Export PPTX 导出PPT',
  building: 'Building PPT…',
  downloaded: 'PPTX downloaded 已下载',
  disabled: 'Feature disabled — ask admin to enable WEEKLY_DIGEST_PPTX_EXPORT_ENABLED',
  generating: 'Report is still being generated 报告仍在生成中',
};

// Defensive deduplication: keep the first row per product_key. In practice the
// generator already ensures uniqueness via the (report_id, product_key) partial
// unique index, but this guards against any data drift (e.g. partial re-runs).
function dedupeByKey(products) {
  const seen = new Set();
  const out = [];
  for (const p of products || []) {
    if (!p?.product_key || seen.has(p.product_key)) continue;
    seen.add(p.product_key);
    out.push(p);
  }
  return out;
}

export default function ReportDetail({ reportId }) {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportEnabled, setExportEnabled] = useState(true);
  const { toast } = useToast();

  async function handleExportPptx() {
    if (!reportId || exporting) return;
    setExporting(true);
    try {
      const { blob, filename } = await weeklyDigestApi.exportPptx(reportId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast({ description: T.downloaded });
    } catch (e) {
      console.error('PPTX export failed:', e);
      const msg = e?.message || 'Export failed';
      if (/not enabled|feature disabled/i.test(msg)) setExportEnabled(false);
      toast({ variant: 'destructive', description: msg });
    } finally {
      setExporting(false);
    }
  }

  useEffect(() => {
    if (!reportId) { setReport(null); return; }
    let mounted = true;
    setLoading(true);
    (async () => {
      try {
        const data = await weeklyDigestApi.get(reportId);
        if (mounted) setReport(data);
      } catch (e) {
        console.error('Failed to load weekly digest report:', e);
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => { mounted = false; };
  }, [reportId]);

  const products = useMemo(() => dedupeByKey(report?.products || []), [report?.products]);
  const summary = useMemo(() => {
    const withPrice = products.filter((p) => p.current_price != null);
    const up = withPrice.filter((p) => (p.change_pct_7d || 0) > 0).length;
    const down = withPrice.filter((p) => (p.change_pct_7d || 0) < 0).length;
    const flat = withPrice.length - up - down;
    return { up, down, flat, total: products.length, priced: withPrice.length };
  }, [products]);

  if (!reportId) {
    return (
      <div className="flex-1 flex items-center justify-center text-slate-400">
        <div className="text-center">
          <FileText className="h-8 w-8 mx-auto mb-2 text-slate-300" />
          <p className="text-sm">Select a report from the left</p>
          <p className="text-xs mt-1">从左侧选择一份报告</p>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-slate-400">
        <div className="text-center">
          <div className="animate-spin w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full mx-auto mb-2" />
          <p className="text-sm">Loading report...</p>
        </div>
      </div>
    );
  }

  if (!report) {
    return (
      <div className="flex-1 flex items-center justify-center text-slate-400">
        <p className="text-sm">Report not found</p>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto bg-white">
      {/* Ecisco-style title strip — matches the EDIA PPT reference */}
      <div className="sticky top-0 z-10 bg-gradient-to-r from-blue-700 via-blue-600 to-cyan-600 text-white shadow-md">
        <div className="px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <BarChart3 className="h-5 w-5 text-cyan-100" />
            <div>
              <h1 className="text-base font-bold tracking-wide">
                {report.title_zh || 'C5C9 周度总结及预测'}
              </h1>
              <p className="text-[11px] text-cyan-100/80">
                {report.title_en || 'C5C9 Weekly Summary & Forecast'} · Ecisco BI
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4 text-[11px] text-cyan-50">
            <span className="flex items-center gap-1">
              <Calendar className="h-3.5 w-3.5" />
              {report.week_start} ~ {report.week_end}
            </span>
            <span className="px-2 py-0.5 rounded bg-white/15">
              {summary.priced}/{summary.total} priced
            </span>
            <span className="px-2 py-0.5 rounded bg-red-500/80 flex items-center gap-0.5">
              <TrendingUp className="h-3 w-3" />{summary.up}
            </span>
            <span className="px-2 py-0.5 rounded bg-green-600/80 flex items-center gap-0.5">
              <TrendingDown className="h-3 w-3" />{summary.down}
            </span>
            {summary.flat > 0 && (
              <span className="px-2 py-0.5 rounded bg-slate-500/60">→{summary.flat}</span>
            )}
            <button
              type="button"
              onClick={handleExportPptx}
              disabled={exporting || !exportEnabled}
              title={exportEnabled ? T.exportPptx : T.disabled}
              className={`ml-1 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[11px] font-semibold shadow-sm transition-all cursor-pointer
                ${exportEnabled
                  ? 'bg-white/90 text-blue-700 hover:bg-white hover:shadow-md active:scale-95'
                  : 'bg-white/40 text-white/60 cursor-not-allowed'}`}
            >
              {exporting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <FileDown className="h-3.5 w-3.5" />
              )}
              {exporting ? T.building : T.exportPptx}
            </button>
          </div>
        </div>
      </div>

      {report.error_msg && (
        <div className="mx-6 mt-3 px-3 py-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded flex items-center gap-1.5">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          {report.error_msg}
        </div>
      )}

      {/* Market overview — 市场概况 / 原料情况 (LLM narrative) */}
      {report.market_overview && (
        <div className="px-6 pt-4">
          <div className="bg-gradient-to-br from-blue-50 to-slate-50 border border-blue-100 rounded-lg shadow-sm">
            <div className="px-4 py-2.5 border-b border-blue-100 flex items-center gap-2">
              <Globe2 className="h-4 w-4 text-blue-600" />
              <h2 className="text-sm font-semibold text-slate-800">市场概况 Market Overview</h2>
            </div>
            <div className="px-4 py-3">
              <MarkdownText>{report.market_overview}</MarkdownText>
            </div>
          </div>
        </div>
      )}

      {/* Per-product cards */}
      <div className="p-6 space-y-4">
        {products.length === 0 ? (
          <div className="text-center text-sm text-slate-400 py-12">
            <FileText className="h-8 w-8 mx-auto mb-2 text-slate-300" />
            This report has no products yet.
          </div>
        ) : (
          products.map((product) => (
            <ProductCard key={product.product_key || product.id} product={product} />
          ))
        )}
      </div>

      {/* Executive summary — 总结预测及建议 (numbered recommendations) */}
      {report.executive_summary && (
        <div className="px-6 pb-6 pt-2">
          <div className="bg-gradient-to-br from-amber-50 to-slate-50 border border-amber-200 rounded-lg shadow-sm">
            <div className="px-4 py-2.5 border-b border-amber-200 flex items-center gap-2">
              <ListChecks className="h-4 w-4 text-amber-600" />
              <h2 className="text-sm font-semibold text-slate-800">总结预测及建议 Executive Summary</h2>
            </div>
            <div className="px-4 py-3">
              <MarkdownText>{report.executive_summary}</MarkdownText>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
