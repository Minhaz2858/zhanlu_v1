import { base44 } from '@/api/base44Client';

/**
 * EDIA Market Dashboard API client.
 *
 * All endpoints live under /api/market-dashboard/* (registered in
 * backend/main.py). Fail-closed: empty EDIA_MYSQL_URL → HTTP 503 with
 * {"detail": "edia_unavailable"}. The frontend surfaces this as an
 * "EDIA data source offline" banner (see MarketDashboardPage.jsx).
 */
export const marketDashboardApi = {
  enabled: () => base44.get('/market-dashboard/enabled'),
  snapshot: (group) =>
    base44.get('/market-dashboard/snapshot', { params: { group } }),
  history: (productId, days = 30) =>
    base44.get(`/market-dashboard/history/${productId}`, { params: { days } }),
  // Phase E:
  regional: (productId) =>
    base44.get(`/market-dashboard/regional/${productId}`),
  news: (group, limit = 18) =>
    base44.get('/market-dashboard/news', { params: { group, limit } }),
};
