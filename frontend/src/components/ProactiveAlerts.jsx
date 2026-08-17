import { useState, useEffect, useRef, useCallback } from 'react';
import toast from 'react-hot-toast';
import {
  Bell,
  TrendingUp,
  TrendingDown,
  X,
  AlertTriangle,
  ArrowRightLeft,
  Sun,
  Settings,
  ChevronDown,
  ChevronUp,
  Lightbulb,
  VolumeX,
} from 'lucide-react';
import {
  getAlerts,
  markAlertsRead,
  dismissAlert,
  getAlertDiagnostics,
  getAlertPreferences,
  muteProduct,
} from '@/api/alertApi';
import AlertPreferencesModal from '@/components/AlertPreferencesModal';

/** Severity badge color + label */
const SEVERITY_CONFIG = {
  high: { bg: 'bg-red-100', text: 'text-red-700', dot: 'bg-red-500', label: 'High' },
  medium: { bg: 'bg-amber-100', text: 'text-amber-700', dot: 'bg-amber-500', label: 'Medium' },
  low: { bg: 'bg-slate-100', text: 'text-slate-600', dot: 'bg-slate-400', label: 'Low' },
};

/** Alert type icon */
function AlertTypeIcon({ type, className = 'h-4 w-4' }) {
  switch (type) {
    case 'price_spike':
      return <TrendingUp className={`${className} text-red-500`} />;
    case 'price_drop':
      return <TrendingDown className={`${className} text-amber-500`} />;
    case 'regional_arbitrage':
      return <ArrowRightLeft className={`${className} text-blue-500`} />;
    default:
      return <AlertTriangle className={`${className} text-slate-500`} />;
  }
}

/** Single alert card */
function AlertCard({ alert, onDismiss, onInvestigate, onMute }) {
  const sev = SEVERITY_CONFIG[alert.severity] || SEVERITY_CONFIG.medium;
  const borderColor =
    alert.severity === 'high'
      ? 'border-l-red-500'
      : alert.severity === 'medium'
        ? 'border-l-amber-500'
        : 'border-l-slate-400';

  const time = alert.triggered_at
    ? new Date(alert.triggered_at).toLocaleString()
    : '';

  // Smart-alert enrichment (payload set by the backend evaluator).
  const payload = alert.payload || {};
  const impact = payload.impact?.impact_yuan_per_day;
  const ctx = payload.ctx || null;
  const recommendation = ctx?.recommendation || null;
  const [whyOpen, setWhyOpen] = useState(false);

  return (
    <div
      className={`relative rounded-xl border-l-4 shadow-sm p-4 gap-3 bg-white ${borderColor}`}
    >
      {/* Mute button */}
      {onMute && (
        <button
          onClick={() => onMute(alert)}
          className="absolute top-2.5 right-8 p-0.5 text-slate-400 hover:text-rose-600 transition-colors rounded"
          aria-label="Mute product"
          title="Mute this product"
        >
          <VolumeX className="h-3.5 w-3.5" />
        </button>
      )}

      {/* Dismiss button */}
      <button
        onClick={() => onDismiss(alert.id)}
        className="absolute top-2.5 right-2.5 p-0.5 text-slate-400 hover:text-slate-600 transition-colors rounded"
        aria-label="Dismiss alert"
      >
        <X className="h-3.5 w-3.5" />
      </button>

      {/* Header: icon + product name + severity badge */}
      <div className="flex items-center gap-2 mb-3">
        <AlertTypeIcon type={alert.alert_type} />
        <span className="text-sm font-semibold text-slate-800 truncate flex-1">
          {alert.product_name || alert.product_id}
        </span>
        <span
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium ${sev.bg} ${sev.text}`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${sev.dot}`} />
          {sev.label}
        </span>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 gap-2 mb-3 text-xs">
        {alert.price != null && (
          <div>
            <span className="text-slate-500">Price</span>
            <p className="font-semibold text-slate-800">{alert.price.toFixed(2)}</p>
          </div>
        )}
        {alert.change_7d_pct != null && (
          <div>
            <span className="text-slate-500">7d Change</span>
            <p
              className={`font-semibold ${
                (alert.change_7d_pct || 0) >= 0 ? 'text-red-600' : 'text-amber-600'
              }`}
            >
              {(alert.change_7d_pct || 0) >= 0 ? '+' : ''}
              {alert.change_7d_pct?.toFixed(1)}%
            </p>
          </div>
        )}
      </div>

      {/* ¥/day business impact chip */}
      {payload.smart && (
        <div className="mb-3">
          {impact != null ? (
            <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-full px-2.5 py-1">
              <TrendingUp className="h-3 w-3" />
              ¥
              {Math.abs(impact).toLocaleString(undefined, {
                maximumFractionDigits: 0,
              })}
              /day impact
            </span>
          ) : (
            <span className="inline-flex items-center text-[11px] font-medium text-slate-400 bg-slate-50 border border-slate-200 rounded-full px-2.5 py-1">
              impact TBD
            </span>
          )}
        </div>
      )}

      {/* Why this matters — expandable */}
      {ctx?.why && (
        <div className="mb-3 rounded-lg bg-indigo-50/60 border border-indigo-100 overflow-hidden">
          <button
            onClick={() => setWhyOpen((v) => !v)}
            className="w-full flex items-center justify-between px-3 py-2 text-[11px] font-semibold text-indigo-700 hover:bg-indigo-100/60 transition-colors"
            aria-label="Why this matters"
            aria-expanded={whyOpen}
          >
            <span>Why this matters</span>
            {whyOpen ? (
              <ChevronUp className="h-3.5 w-3.5" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" />
            )}
          </button>
          {whyOpen && (
            <p className="px-3 pb-2.5 text-[11px] text-slate-600 leading-relaxed">
              {ctx.why}
            </p>
          )}
        </div>
      )}

      {/* Suggested next step */}
      {recommendation && (
        <div className="mb-3 rounded-lg border border-emerald-200 bg-emerald-50/60 px-3 py-2.5">
          <p className="flex items-center gap-1 text-[11px] font-semibold text-emerald-800 mb-1">
            <Lightbulb className="h-3.5 w-3.5 flex-shrink-0" />
            {recommendation.title}
          </p>
          {Array.isArray(recommendation.steps) &&
            recommendation.steps.length > 0 && (
              <ul className="list-disc pl-4 text-[11px] text-slate-600 space-y-0.5">
                {recommendation.steps.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ul>
            )}
        </div>
      )}

      {/* Message */}
      {alert.message && (
        <p className="text-xs text-slate-600 mb-3 leading-relaxed">{alert.message}</p>
      )}

      {/* Source + time */}
      <div className="flex items-center justify-between text-[11px] text-slate-400 mb-3">
        <span>{alert.source || 'perception'}</span>
        {time && <span>{time}</span>}
      </div>

      {/* Investigate button */}
      <button
        onClick={() => onInvestigate(alert)}
        className="w-full bg-slate-800 hover:bg-slate-700 text-white text-xs font-semibold py-2.5 rounded-lg transition-colors"
      >
        Investigate
      </button>
    </div>
  );
}

/** Empty state */
function EmptyAlerts({ onScan }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-6 text-center">
      <div className="w-14 h-14 rounded-full bg-slate-100 flex items-center justify-center mb-4">
        <Bell className="h-6 w-6 text-slate-400" />
      </div>
      <p className="text-sm font-medium text-slate-600 mb-1">No market alerts</p>
      <p className="text-xs text-slate-400 mb-4">
        We monitor price movements and arbitrage opportunities automatically.
      </p>
      <button
        onClick={onScan}
        className="text-xs font-medium text-slate-600 hover:text-slate-800 underline underline-offset-2 transition-colors"
      >
        Scan now
      </button>
    </div>
  );
}

/**
 * Compact diagnostics banner — explains *why* the panel may show zero alerts.
 * Rendered from GET /api/alerts/diagnostics, not on every poll.
 */
function DiagnosticsBanner({ diagnostics }) {
  if (!diagnostics) return null;

  const { flags, warehouse, thresholds, triggering_products, effective_org_id } =
    diagnostics;

  const alertsOff = flags && flags.ecisco_bi_alerts_enabled === false;
  const unreachable = warehouse && warehouse.configured && warehouse.reachable === false;
  const notConfigured = warehouse && warehouse.configured === false;
  const triggerCount = triggering_products ? triggering_products.length : 0;

  // Only show the banner when something is worth explaining.
  if (!alertsOff && !unreachable && !notConfigured) return null;

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-[11px] text-amber-800 space-y-1">
      <div className="flex items-center gap-1.5 font-semibold">
        <AlertTriangle className="h-3.5 w-3.5" />
        <span>Pipeline diagnostic</span>
      </div>
      {alertsOff && (
        <p>Alerts flag is OFF — enable <code>ECISCO_BI_ALERTS_ENABLED</code> to scan.</p>
      )}
      {notConfigured && (
        <p>EDIA warehouse not configured (<code>EDIA_MYSQL_URL</code> missing).</p>
      )}
      {unreachable && (
        <p>EDIA warehouse unreachable — no prices can be scanned.</p>
      )}
      <p className="text-amber-700">
        Thresholds: spike ≥{thresholds?.price_spike}% / drop ≤{thresholds?.price_drop}%
        / high ≥{thresholds?.high_severity}%. Org: <code>{effective_org_id}</code>.
        {triggerCount > 0 && <> Would-trigger now: {triggerCount} product(s).</>}
      </p>
    </div>
  );
}

/** Morning Brief card — pinned digest card for daily_digest alerts */
function MorningBriefCard({ alert, onInvestigate }) {
  const payload = alert.payload || {};
  const items = payload.items || [];

  return (
    <div className="relative rounded-xl border-l-4 border-l-indigo-500 shadow-sm p-4 bg-gradient-to-br from-indigo-50 to-white">
      {/* Header */}
      <div className="flex items-center gap-2 mb-3">
        <div className="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center">
          <Sun className="h-4 w-4 text-white" />
        </div>
        <div>
          <h3 className="text-sm font-bold text-slate-800">晨报 Morning Brief</h3>
          <p className="text-[11px] text-slate-400">{payload.date || ''}</p>
        </div>
      </div>

      {/* Narrative */}
      {alert.message && (
        <div className="text-xs text-slate-700 leading-relaxed mb-3 whitespace-pre-line max-h-40 overflow-y-auto">
          {alert.message}
        </div>
      )}

      {/* Top items with ¥ impact */}
      {items.length > 0 && (
        <div className="space-y-1.5 mb-3">
          {items.slice(0, 3).map((item, i) => (
            <div key={i} className="flex items-center justify-between text-xs">
              <div className="flex items-center gap-1.5 min-w-0 flex-1">
                <AlertTypeIcon type={item.alert_type} className="h-3 w-3 flex-shrink-0" />
                <span className="text-slate-600 truncate">{item.product_name || 'Unknown'}</span>
              </div>
              <span className="font-semibold text-slate-800 ml-2 flex-shrink-0">
                {item.impact_yuan_per_day != null
                  ? `¥${Math.abs(item.impact_yuan_per_day).toLocaleString(undefined, { maximumFractionDigits: 0 })}/day`
                  : '—'}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Investigate button */}
      <button
        onClick={() => onInvestigate(alert)}
        className="w-full bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-semibold py-2.5 rounded-lg transition-colors"
      >
        展开详情
      </button>
    </div>
  );
}

/* ===================================================================
 * ProactiveAlerts — slide-out panel
 * =================================================================== */
export default function ProactiveAlerts({
  open,
  onClose,
  onInvestigate,
}) {
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [scanning, setScanning] = useState(false);
  const [diagnostics, setDiagnostics] = useState(null);
  const [scanResult, setScanResult] = useState(null);
  const [prefsOpen, setPrefsOpen] = useState(false);
  const [prefs, setPrefs] = useState(null);
  const pendingDismissRef = useRef(new Set());

  const fetchAlerts = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getAlerts();
      setAlerts(data.alerts || []);
      setError(null);
    } catch (err) {
      console.error('Failed to fetch alerts:', err);
      setError('Could not load alerts');
    } finally {
      setLoading(false);
    }
  }, []);

  // Diagnostics fetched once on open (and after manual scans) — NOT on every poll.
  const fetchDiagnostics = useCallback(async () => {
    try {
      const data = await getAlertDiagnostics();
      setDiagnostics(data);
    } catch (err) {
      console.error('Failed to fetch alert diagnostics:', err);
      setDiagnostics(null);
    }
  }, []);

  // Fetch on open
  useEffect(() => {
    if (open) {
      fetchAlerts();
      fetchDiagnostics();
      getAlertPreferences()
        .then(setPrefs)
        .catch((err) => {
          console.error('Failed to load alert preferences:', err);
          setPrefs(null);
        });
    }
  }, [open, fetchAlerts, fetchDiagnostics]);

  // Polling every 2 minutes while open
  useEffect(() => {
    if (!open) return;
    const id = setInterval(fetchAlerts, 2 * 60 * 1000);
    return () => clearInterval(id);
  }, [open, fetchAlerts]);

  // Mark all read when panel opens
  useEffect(() => {
    if (open && alerts.length > 0) {
      markAlertsRead().catch(() => {});
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  // Escape key closes
  useEffect(() => {
    const handler = (e) => {
      if (e.key === 'Escape' && open) onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [open, onClose]);

  const handleDismiss = async (alertId) => {
    pendingDismissRef.current.add(alertId);
    setAlerts((prev) => prev.filter((a) => a.id !== alertId));
    try {
      await dismissAlert(alertId);
    } catch (err) {
      console.error('Failed to dismiss alert:', err);
    } finally {
      pendingDismissRef.current.delete(alertId);
    }
  };

  const handleMute = async (alert) => {
    const current = prefs?.muted_products || [];
    try {
      const updated = await muteProduct(alert.product_id, current);
      setPrefs(updated);
      toast.success(`${alert.product_name || alert.product_id} muted`);
    } catch (err) {
      console.error('Failed to mute product:', err);
      toast.error('Could not mute product');
    }
  };

  const handleScan = async () => {
    const { scanAlerts: scanFn } = await import('@/api/alertApi');
    setScanning(true);
    setScanResult(null);
    try {
      const result = await scanFn();
      setScanResult(result);
      await Promise.all([fetchAlerts(), fetchDiagnostics()]);
    } catch (err) {
      console.error('Scan failed:', err);
      setScanResult({ error: err?.message || 'Scan failed' });
    } finally {
      setScanning(false);
    }
  };

  if (!open) return null;

  const highCount = alerts.filter((a) => a.severity === 'high').length;
  const medCount = alerts.filter((a) => a.severity === 'medium').length;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/30 backdrop-blur-sm z-40 transition-opacity"
        onClick={onClose}
      />

      {/* Panel */}
      <div className="fixed top-0 right-0 h-full w-96 bg-white border-l border-slate-200 shadow-2xl z-50 flex flex-col animate-slide-in-right">
        {/* Header */}
        <div className="flex-shrink-0 px-5 py-4 border-b border-slate-100">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-lg bg-slate-900 flex items-center justify-center">
                <Bell className="h-4.5 w-4.5 text-white" />
              </div>
              <div>
                <h2 className="text-sm font-bold text-slate-800">Market Alerts</h2>
                <p className="text-[11px] text-slate-400">
                  Proactive price &amp; arbitrage monitoring
                </p>
              </div>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPrefsOpen(true)}
                className="p-1.5 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-lg transition-colors"
                aria-label="Alert preferences"
                title="Alert preferences"
              >
                <Settings className="h-4 w-4" />
              </button>
              <button
                onClick={onClose}
                className="p-1.5 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-lg transition-colors"
                aria-label="Close alerts"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          {/* Severity counts */}
          <div className="flex items-center gap-3">
            {highCount > 0 && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-red-600">
                <span className="w-2 h-2 rounded-full bg-red-500" />
                {highCount} high
              </span>
            )}
            {medCount > 0 && (
              <span className="inline-flex items-center gap-1 text-[11px] font-medium text-amber-600">
                <span className="w-2 h-2 rounded-full bg-amber-500" />
                {medCount} medium
              </span>
            )}
            <span className="text-[11px] text-slate-400">
              {alerts.length} total
            </span>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {/* Diagnostics banner — explains zero-alert states */}
          <DiagnosticsBanner diagnostics={diagnostics} />

          {/* Scan result feedback */}
          {scanResult && (
            <div
              className={`rounded-lg border px-3 py-2.5 text-[11px] space-y-1 ${
                scanResult.error
                  ? 'border-red-200 bg-red-50 text-red-700'
                  : 'border-emerald-200 bg-emerald-50 text-emerald-700'
              }`}
            >
              <p className="font-semibold">
                {scanResult.error ? 'Scan failed' : 'Scan complete'}
              </p>
              {!scanResult.error && (
                <>
                  <p>
                    {scanResult.new_alerts != null && (
                      <>New alerts: {scanResult.new_alerts} · </>
                    )}
                    Scanned: {scanResult.total_scanned ?? 0} product(s)
                    {scanResult.reachable != null && (
                      <> · Warehouse: {scanResult.reachable ? 'reachable' : 'UNREACHABLE'}</>
                    )}
                  </p>
                  {scanResult.skipped && scanResult.reason && (
                    <p className="opacity-80">Skipped: {scanResult.reason}</p>
                  )}
                </>
              )}
              {scanResult.error && (
                <p className="opacity-80">{scanResult.error}</p>
              )}
            </div>
          )}

          {loading && alerts.length === 0 && (
            <div className="flex items-center justify-center py-16">
              <div className="animate-spin rounded-full h-6 w-6 border-2 border-slate-300 border-t-slate-600" />
            </div>
          )}

          {error && !loading && (
            <div className="text-center py-8">
              <p className="text-sm text-red-500 mb-2">{error}</p>
              <button
                onClick={fetchAlerts}
                className="text-xs font-medium text-slate-600 hover:text-slate-800"
              >
                Retry
              </button>
            </div>
          )}

          {!loading && !error && alerts.length === 0 && (
            <EmptyAlerts onScan={handleScan} />
          )}

          {/* Morning Brief digest card — pinned at top */}
          {alerts.filter((a) => a.alert_type === 'daily_digest').map((alert) => (
            <MorningBriefCard
              key={alert.id}
              alert={alert}
              onInvestigate={onInvestigate}
            />
          ))}

          {/* Regular alerts */}
          {alerts.filter((a) => a.alert_type !== 'daily_digest').map((alert) => (
            <AlertCard
              key={alert.id}
              alert={alert}
              onDismiss={handleDismiss}
              onInvestigate={onInvestigate}
              onMute={handleMute}
            />
          ))}
        </div>

        {/* Footer: scan button */}
        <div className="flex-shrink-0 px-5 py-3 border-t border-slate-100">
          <button
            onClick={handleScan}
            disabled={scanning}
            className="w-full flex items-center justify-center gap-2 py-2 text-xs font-medium text-slate-500 hover:text-slate-700 hover:bg-slate-50 rounded-lg transition-colors disabled:opacity-50"
          >
            {scanning ? (
              <>
                <div className="animate-spin rounded-full h-3 w-3 border border-slate-400 border-t-transparent" />
                Scanning...
              </>
            ) : (
              'Scan for alerts'
            )}
          </button>
        </div>
      </div>

      {/* Preferences modal */}
      <AlertPreferencesModal
        open={prefsOpen}
        onClose={() => setPrefsOpen(false)}
        onSaved={setPrefs}
      />
    </>
  );
}
