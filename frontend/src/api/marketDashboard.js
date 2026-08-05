import { authFetch } from '@/api/authFetch';

/**
 * EDIA Market Dashboard API client.
 *
 * Hits the local FastAPI routes under /api/market-dashboard/* (registered
 * in backend/main.py). Uses `authFetch` (NOT `base44.get`) so the user's
 * JWT is attached and 401-triggered token refresh works.
 *
 * Fail-closed: empty EDIA_MYSQL_URL → HTTP 503 with
 * {"detail": "edia_unavailable"} — we surface that as a thrown Error so
 * the page can show the "data source offline" banner.
 */
async function getJson(path, params) {
  const qs = new URLSearchParams();
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) qs.set(k, String(v));
    }
  }
  const url = qs.toString() ? `${path}?${qs}` : path;
  const res = await authFetch(url, { method: 'GET' });
  if (!res.ok) {
    let detail;
    try {
      const body = await res.json();
      detail = body?.detail;
    } catch {
      /* ignore parse errors */
    }
    throw new Error(detail || `market-dashboard ${res.status}`);
  }
  return res.json();
}

export const marketDashboardApi = {
  enabled: () => getJson('/api/market-dashboard/enabled'),
  snapshot: (group) => getJson('/api/market-dashboard/snapshot', { group }),
  history: (productId, days = 30) =>
    getJson(`/api/market-dashboard/history/${productId}`, { days }),
  regional: (productId) =>
    getJson(`/api/market-dashboard/regional/${productId}`),
  regionalBatch: (productIds) =>
    getJson('/api/market-dashboard/regional-batch', {
      product_ids: productIds.join(','),
    }),
  forecastBaseBatch: (productIds, day = 7) =>
    getJson('/api/market-dashboard/forecast-base-batch', {
      product_ids: productIds.join(','),
      day,
    }),
  decisionBoard: (group, day = 7) =>
    getJson('/api/market-dashboard/decision-board', { group, day }),
  /** Single-shot bundle for the chemical market overview page.
   *  Backend has a 30-second in-memory cache; pass noCache=true to bypass. */
  overview: ({ noCache = false, forecastDay = 7 } = {}) =>
    getJson('/api/market-dashboard/overview', {
      no_cache: noCache ? 1 : 0,
      forecast_day: forecastDay,
    }),
  /** Drop the in-memory overview cache (returns {invalidated, key_prefix}). */
  refreshOverview: async () => {
    const res = await authFetch('/api/market-dashboard/overview/refresh', {
      method: 'POST',
    });
    if (!res.ok) {
      throw new Error(`overview refresh failed: ${res.status}`);
    }
    return res.json();
  },
  events: (hours = 168, reviewStatus = 'approved', minUsefulnessScore = 0) =>
    getJson('/api/market-dashboard/events/latest', {
      hours,
      review_status: reviewStatus,
      min_usefulness_score: minUsefulnessScore,
    }),
  fxRate: () => getJson('/api/market-dashboard/fx/usd-cny'),
  news: (group, limit = 18) =>
    getJson('/api/market-dashboard/news', { group, limit }),
  /** Upstream page: merged past+forecast timeline for a product. */
  forecastChart: (productId, days = 30) =>
    getJson(`/api/market-dashboard/forecast-chart/${productId}`, { days }),
  /** List all SKU targets for an ERP product family (c5_resin, raffinate_c5). */
  forecastListSkus: (productId) =>
    getJson('/api/market-dashboard/forecast-skus', { product_id: productId }),
  /** Upstream page: 3-tier price transmission chain. */
  priceChain: () => getJson('/api/market-dashboard/price-chain'),
  /** HITL: submit a price correction (pending until scored against actuals). */
  async submitForecastFeedback({ product_id, ai_price, user_price, reason, target_date }) {
    const res = await authFetch('/api/market-dashboard/forecast-feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_id, ai_price, user_price, reason, target_date }),
    });
    if (!res.ok) throw new Error(`feedback submit failed: ${res.status}`);
    return res.json();
  },
  /** HITL: list correction history for a product. */
  async listForecastFeedback(product_id) {
    const res = await authFetch(
      `/api/market-dashboard/forecast-feedback?product_id=${encodeURIComponent(product_id)}`,
    );
    if (!res.ok) return [];
    return res.json();
  },
};
