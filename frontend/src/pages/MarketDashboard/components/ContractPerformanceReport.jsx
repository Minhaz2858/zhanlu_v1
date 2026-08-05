import { useState, useCallback, useRef, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  TrendingUp, TrendingDown, Minus, Loader2, Search, RotateCcw, Sparkles,
} from 'lucide-react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip as RcTooltip,
  ResponsiveContainer, Cell, Legend,
} from 'recharts';
import { reportsApi } from '@/api/reports';
import { TYPO } from '../lib/typography';
import { getChartTheme, rechartsTooltipStyle } from '../lib/chartTheme';
import { useLanguage } from '@/lib/LanguageProvider';

const BAR_COLORS = ['#3B82F6', '#6366F1', '#8B5CF6', '#A855F7', '#0EA5E9'];
const EXEC_FAIL_COLOR = '#EF4444';

function YoYBadge({ yoy }) {
  if (yoy == null || yoy === 0) {
    return <Minus className="inline h-3.5 w-3.5 text-slate-400" />;
  }
  const up = yoy > 0;
  const color = up ? 'text-red-600 bg-red-50' : 'text-green-600 bg-green-50';
  const Icon = up ? TrendingUp : TrendingDown;
  return (
    <span className={`inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-xs font-medium ${color}`}>
      <Icon className="h-3 w-3" />
      {Math.abs(yoy).toFixed(1)}%
    </span>
  );
}

export default function ContractPerformanceReport({ org }) {
  const { lang } = useLanguage();
  const t = lang === 'zh'
    ? {
        startDate: '起始日期',
        endDate: '截止日期',
        material: '物料',
        allMaterials: '所有物料',
        query: '查询',
        reset: '重置',
        dataRange: '数据区间',
        noDataHint: '请选择日期范围后点击「查询」加载合同执行数据',
        loading: '加载中…',
        contractAmount: '合同金额',
        unitYi: '亿元',
        unfulfilledOrders: '未出库订单',
        unitOrder: '份',
        completionRate: '完成率',
        unitPct: '%',
        invoiceAmount: '开票金额',
        unitWan: '万元',
        contractQty: '合同量',
        unitTon: '吨',
        outQty: '出库量',
        notOutQty: '未出量',
        uninvoicedAmount: '未开票金额',
        contractExec: '合同执行情况',
        aiBrief: 'AI 整体简评',
        aiError: 'AI 分析出错',
        unfulfilledContracts: '未出库合同',
        colProduct: '产品',
        colContractNo: '合同号',
        colCustomer: '客户',
        colDeliveryDate: '交货日期',
        colContractQtyTon: '合同量 (吨)',
        colNotOutQtyTon: '未出量 (吨)',
        colCompletion: '完成率',
        colStatus: '状态',
        colAmountWan: '金额 (万元)',
      }
    : {
        startDate: 'Start Date',
        endDate: 'End Date',
        material: 'Material',
        allMaterials: 'All materials',
        query: 'Query',
        reset: 'Reset',
        dataRange: 'Data range',
        noDataHint: 'Select a date range and click "Query" to load contract data',
        loading: 'Loading…',
        contractAmount: 'Contract Amount',
        unitYi: '100M CNY',
        unfulfilledOrders: 'Unfulfilled Orders',
        unitOrder: 'orders',
        completionRate: 'Completion Rate',
        unitPct: '%',
        invoiceAmount: 'Invoice Amount',
        unitWan: '10k CNY',
        contractQty: 'Contract Qty',
        unitTon: 'ton',
        outQty: 'Out Qty',
        notOutQty: 'Not-out Qty',
        uninvoicedAmount: 'Uninvoiced Amount',
        contractExec: 'Contract Performance',
        aiBrief: 'AI Overall Brief',
        aiError: 'AI analysis error',
        unfulfilledContracts: 'Unfulfilled Contracts',
        colProduct: 'Product',
        colContractNo: 'Contract No.',
        colCustomer: 'Customer',
        colDeliveryDate: 'Delivery Date',
        colContractQtyTon: 'Contract Qty (ton)',
        colNotOutQtyTon: 'Not-out Qty (ton)',
        colCompletion: 'Completion',
        colStatus: 'Status',
        colAmountWan: 'Amount (10k CNY)',
      };
  const today = new Date().toISOString().slice(0, 10);
  const thirtyDaysAgo = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);

  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [material, setMaterial] = useState('');
  const [queried, setQueried] = useState(false);

  // AI State
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState('');
  const [aiChartTop, setAiChartTop] = useState('');
  const [aiChartBottom, setAiChartBottom] = useState('');
  const [aiSummaryParagraph, setAiSummaryParagraph] = useState('');
  const [aiBullets, setAiBullets] = useState([]);
  const [aiUnshippedAnalysis, setAiUnshippedAnalysis] = useState('');
  const [aiDone, setAiDone] = useState(false);
  const aiSeqRef = useRef(0);
  const abortRef = useRef(null);

  // Actual query params after user clicks Search
  const [queryParams, setQueryParams] = useState({
    org,
    start_date: '',
    end_date: '',
    material: '',
  });

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['reports', 'contract-performance', queryParams],
    queryFn: () => reportsApi.contractPerformance(queryParams),
    staleTime: 60_000,
    enabled: queried,
  });

  const availableMaterials = data?.available_materials || [];

  // ── AI Streaming ──
  const runAiAnalysis = useCallback(() => {
    const seq = ++aiSeqRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setAiLoading(true);
    setAiError('');
    setAiChartTop('');
    setAiChartBottom('');
    setAiSummaryParagraph('');
    setAiBullets([]);
    setAiUnshippedAnalysis('');
    setAiDone(false);

    const params = {
      org: queryParams.org || org,
      start_date: queryParams.start_date,
      end_date: queryParams.end_date,
      material: queryParams.material,
    };

    reportsApi.streamAiAnalysis(
      params,
      {
        onMeta: (status) => {
          if (seq !== aiSeqRef.current) return;
          if (status === 'cached') setAiDone(true);
        },
        onDelta: (field, text) => {
          if (seq !== aiSeqRef.current) return;
          setAiDone(false);
          switch (field) {
            case 'chart_top': setAiChartTop((p) => p + text); break;
            case 'chart_bottom': setAiChartBottom((p) => p + text); break;
            case 'summary_paragraph': setAiSummaryParagraph((p) => p + text); break;
            case 'unshipped_analysis': setAiUnshippedAnalysis((p) => p + text); break;
          }
        },
        onBullets: (bullets) => {
          if (seq !== aiSeqRef.current) return;
          setAiBullets(bullets);
        },
        onDone: (result) => {
          if (seq !== aiSeqRef.current) return;
          // Fill any missing fields from the result object
          if (result.chart_top) setAiChartTop(result.chart_top);
          if (result.chart_bottom) setAiChartBottom(result.chart_bottom);
          if (result.summary_paragraph) setAiSummaryParagraph(result.summary_paragraph);
          if (result.summary_bullets?.length) setAiBullets(result.summary_bullets);
          if (result.unshipped_analysis) setAiUnshippedAnalysis(result.unshipped_analysis);
          setAiDone(true);
          setAiLoading(false);
        },
        onError: (msg) => {
          if (seq !== aiSeqRef.current) return;
          setAiError(msg);
          setAiLoading(false);
        },
      },
      controller.signal,
    ).catch((e) => {
      if (e?.name !== 'AbortError') {
        console.error('AI analysis stream error:', e);
        setAiError(e?.message || 'Stream error');
        setAiLoading(false);
      }
    });
  }, [org, queryParams]);

  // Auto-run AI analysis when data loads
  useEffect(() => {
    if (data && queried) {
      runAiAnalysis();
    }
    return () => { abortRef.current?.abort(); };
  }, [data, queried]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSearch = () => {
    setQueryParams({ org, start_date: startDate, end_date: endDate, material });
    setQueried(true);
  };

  const handleReset = () => {
    setStartDate('');
    setEndDate('');
    setMaterial('');
    setQueried(false);
  };

  // KPI Card
  const KpiCard = ({ title, value, unit, yoy }) => (
    <div className="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
      <div className={TYPO.secondaryData}>{title}</div>
      <div className="mt-0.5 flex items-baseline gap-1">
        <span className="text-lg font-bold font-mono text-slate-900 dark:text-white">
          {value != null ? Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 }) : '--'}
        </span>
        {unit && <span className="text-xs text-slate-500">{unit}</span>}
      </div>
      <div className="mt-1">
        <YoYBadge yoy={yoy} />
      </div>
    </div>
  );

  // ── Render ──

  const theme = getChartTheme(false);
  const ttStyle = rechartsTooltipStyle(theme, false);

  return (
    <div className="space-y-6">
      {/* Filters */}
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-slate-500 mb-1">{t.startDate}</label>
          <input
            type="date"
            value={startDate || thirtyDaysAgo}
            onChange={(e) => setStartDate(e.target.value)}
            className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-700 focus:border-slate-400 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300"
          />
        </div>
        <div>
          <label className="block text-xs text-slate-500 mb-1">{t.endDate}</label>
          <input
            type="date"
            value={endDate || today}
            onChange={(e) => setEndDate(e.target.value)}
            className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-700 focus:border-slate-400 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300"
          />
        </div>
        <div>
          <label className="block text-xs text-slate-500 mb-1">{t.material}</label>
          <select
            value={material}
            onChange={(e) => setMaterial(e.target.value)}
            className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-700 focus:border-slate-400 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300"
          >
            <option value="">{t.allMaterials}</option>
            {availableMaterials.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </div>
        <button
          onClick={handleSearch}
          className="inline-flex items-center gap-1.5 rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 transition-colors dark:bg-white dark:text-slate-900 dark:hover:bg-slate-200"
        >
          <Search className="h-3.5 w-3.5" />
          {t.query}
        </button>
        <button
          onClick={handleReset}
          className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50 transition-colors dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700"
        >
          <RotateCcw className="h-3.5 w-3.5" />
          {t.reset}
        </button>
        {queried && (
          <span className={TYPO.secondaryData + ' self-end pb-1.5'}>
            数据区间: {queryParams.start_date || 'auto'} ~ {queryParams.end_date || 'auto'}
          </span>
        )}
      </div>

      {!queried && (
        <div className="rounded border border-slate-200 bg-slate-50 p-8 text-center text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
          {t.noDataHint}
        </div>
      )}

      {isLoading && (
        <div className="py-8 text-center text-sm text-slate-500">
          <Loader2 className="inline h-4 w-4 animate-spin mr-1" />
          {t.loading}
        </div>
      )}

      {isError && (
        <div className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
          Failed: {error?.message || 'unknown'}
        </div>
      )}

      {data && queried && !isLoading && (
        <>
          {/* KPI Cards — 2 rows of 4 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <KpiCard title={t.contractAmount} value={data.summary.contract_amount_yi} unit={t.unitYi} yoy={data.summary.amount_yoy} />
            <KpiCard title={t.unfulfilledOrders} value={data.summary.unfulfilled_orders} unit={t.unitOrder} yoy={data.summary.unfulfilled_yoy} />
            <KpiCard title={t.completionRate} value={data.summary.completion_rate} unit={t.unitPct} yoy={data.summary.completion_rate_yoy} />
            <KpiCard title={t.invoiceAmount} value={data.summary.invoice_amount_wan} unit={t.unitWan} yoy={data.summary.invoice_yoy} />
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <KpiCard title={t.contractQty} value={data.summary.contract_qty} unit={t.unitTon} yoy={data.summary.contract_qty_yoy} />
            <KpiCard title={t.outQty} value={data.summary.out_qty} unit={t.unitTon} yoy={data.summary.out_qty_yoy} />
            <KpiCard title={t.notOutQty} value={data.summary.not_out_qty} unit={t.unitTon} yoy={data.summary.not_out_qty_yoy} />
            <KpiCard title={t.uninvoicedAmount} value={data.summary.uninvoiced_amount_wan} unit={t.unitWan} yoy={data.summary.uninvoiced_yoy} />
          </div>

          {/* Execution Chart + AI Panel - side by side */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* Chart column */}
            <div className="lg:col-span-2 space-y-4">
              {/* AI chart top */}
              {aiChartTop && (
                <div className="rounded bg-blue-50 px-3 py-2 text-sm text-blue-800 dark:bg-blue-950 dark:text-blue-300">
                  <Sparkles className="inline h-3 w-3 mr-1" />
                  {aiChartTop}
                  {aiLoading && !aiDone && <span className="inline-block w-1.5 h-4 ml-0.5 bg-blue-500 animate-pulse rounded-sm" />}
                </div>
              )}

              <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
                <div className={TYPO.cardTitle}>{t.contractExec}</div>
                <div className="mt-2" style={{ width: '100%', height: 300 }}>
                  <ResponsiveContainer>
                    <BarChart
                      data={(data.execution_chart || []).slice(0, 12)}
                      layout="vertical"
                      margin={{ left: 10, right: 10 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
                      <XAxis type="number" tick={{ fontSize: 11, fill: theme.tick }} />
                      <YAxis
                        type="category"
                        dataKey="product"
                        width={100}
                        tick={{ fontSize: 11, fill: theme.tick }}
                      />
                      <RcTooltip
                        contentStyle={ttStyle}
                        formatter={(v, name) => [`${Number(v).toLocaleString()} ${t.unitTon}`, name === 'contract_qty' ? t.contractQty : t.outQty]}
                      />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      <Bar dataKey="contract_qty" name={t.contractQty} fill={BAR_COLORS[0]} barSize={16} />
                      <Bar dataKey="out_qty" name={t.outQty} fill={BAR_COLORS[2]} barSize={16} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>

              {/* AI chart bottom */}
              {aiChartBottom && (
                <div className="rounded bg-blue-50 px-3 py-2 text-sm text-blue-800 dark:bg-blue-950 dark:text-blue-300">
                  <Sparkles className="inline h-3 w-3 mr-1" />
                  {aiChartBottom}
                  {aiLoading && !aiDone && <span className="inline-block w-1.5 h-4 ml-0.5 bg-blue-500 animate-pulse rounded-sm" />}
                </div>
              )}
            </div>

            {/* AI Side Panel */}
            <div className="space-y-4">
              <div className="rounded-lg border border-indigo-200 bg-white p-4 dark:border-indigo-800 dark:bg-slate-800">
                <div className="flex items-center gap-2 mb-3">
                  <Sparkles className="h-4 w-4 text-indigo-500" />
                  <span className="text-sm font-semibold text-indigo-700 dark:text-indigo-400">{t.aiBrief}</span>
                  {aiLoading && !aiDone && (
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-indigo-400" />
                  )}
                </div>

                {aiError && (
                  <div className="text-xs text-red-600 dark:text-red-400 mb-2">
                    {t.aiError}: {aiError}
                  </div>
                )}

                {aiSummaryParagraph && (
                  <p className="text-sm text-slate-700 dark:text-slate-300 leading-relaxed">
                    {aiSummaryParagraph}
                    {aiLoading && !aiDone && <span className="inline-block w-1.5 h-4 ml-0.5 bg-indigo-500 animate-pulse rounded-sm" />}
                  </p>
                )}

                {aiBullets.length > 0 && (
                  <ul className="mt-3 space-y-1.5">
                    {aiBullets.map((b, i) => (
                      <li key={i} className="flex gap-2 text-sm text-slate-600 dark:text-slate-400">
                        <span className="text-indigo-400 mt-0.5 shrink-0">•</span>
                        <span>{b}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </div>

          {/* Unshipped Contracts Table */}
          {data.unshipped_contracts && data.unshipped_contracts.length > 0 && (
            <div className="rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-800 overflow-hidden">
              <div className="border-b border-slate-200 p-4 dark:border-slate-700">
                <span className={TYPO.cardTitle}>{t.unfulfilledContracts}</span>
                <span className={`ml-2 ${TYPO.secondaryData}`}>≥30吨 | Top 15</span>
              </div>

              {/* AI unshipped analysis */}
              {aiUnshippedAnalysis && (
                <div className="mx-4 mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">
                  <Sparkles className="inline h-3 w-3 mr-1" />
                  {aiUnshippedAnalysis}
                  {aiLoading && !aiDone && <span className="inline-block w-1.5 h-4 ml-0.5 bg-red-500 animate-pulse rounded-sm" />}
                </div>
              )}

              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-800">
                      <th className="px-4 py-2 text-left text-xs font-semibold text-slate-600 dark:text-slate-400">{t.colProduct}</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-slate-600 dark:text-slate-400">{t.colContractNo}</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-slate-600 dark:text-slate-400">{t.colCustomer}</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-slate-600 dark:text-slate-400">{t.colDeliveryDate}</th>
                      <th className="px-4 py-2 text-right text-xs font-semibold text-slate-600 dark:text-slate-400">{t.colContractQtyTon}</th>
                      <th className="px-4 py-2 text-right text-xs font-semibold text-slate-600 dark:text-slate-400">{t.colNotOutQtyTon}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.unshipped_contracts.map((c, i) => (
                      <tr
                        key={`${c.bill_no}-${i}`}
                        className="border-b border-slate-100 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-750"
                      >
                        <td className="px-4 py-2 text-slate-800 dark:text-slate-200">{c.product}</td>
                        <td className="px-4 py-2 font-mono text-xs text-slate-500 dark:text-slate-400">{c.bill_no}</td>
                        <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{c.customer}</td>
                        <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{c.delivery_date}</td>
                        <td className="px-4 py-2 text-right font-mono text-slate-700 dark:text-slate-300">{Number(c.contract_qty).toLocaleString()}</td>
                        <td className="px-4 py-2 text-right font-mono font-semibold text-red-600 dark:text-red-400">{Number(c.unshipped_qty).toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
