import { authFetch, getAuthToken } from '@/api/authFetch';

/**
 * Report API client for Customer Sales + Contract Performance.
 *
 * Hits /api/reports/* (registered in backend/main.py via reports_router).
 * Uses the same `authFetch`→getJson pattern as marketDashboardApi.
 */
async function getJson(path, params) {
  const qs = new URLSearchParams();
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
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
    throw new Error(detail || `reports ${res.status}`);
  }
  return res.json();
}

/**
 * SSE streaming for contract-performance AI analysis.
 *
 * Protocol (matches backend report_ai_service.py):
 *   meta    → {status: "started"|"cached"}
 *   delta   → {field, text}  incremental text
 *   partial → {summary_bullets: [...]}
 *   done    → AiAnalysisResult
 *   error   → {message}
 *
 * @param {object}   params   Query params (org, start_date, end_date, material)
 * @param {object}   callbacks
 * @param {Function} callbacks.onMeta      (status) => void
 * @param {Function} callbacks.onDelta     (field, text) => void
 * @param {Function} callbacks.onBullets   (bullets) => void
 * @param {Function} callbacks.onDone      (result) => void
 * @param {Function} callbacks.onError     (message) => void
 * @param {AbortSignal} [abortSignal]
 * @returns {Promise<void>}
 */
export async function streamContractAiAnalysis(params, callbacks, abortSignal) {
  const qs = new URLSearchParams({
    org: params.org || 'huizhou',
    start_date: params.start_date || '',
    end_date: params.end_date || '',
    material: params.material || '',
    stream: 'true',
  });
  const url = `/api/reports/contract-performance/ai-analysis?${qs}`;

  const token = getAuthToken();
  const headers = new Headers();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const res = await fetch(url, { method: 'GET', headers, signal: abortSignal });
  if (!res.ok) {
    let detail;
    try { const body = await res.json(); detail = body?.detail; } catch { /* ignore */ }
    callbacks.onError(detail || `AI analysis ${res.status}`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split('\n\n');
      // Keep the last (potentially incomplete) chunk
      buffer = parts.pop() || '';

      for (const part of parts) {
        if (!part.trim()) continue;
        const lines = part.split('\n');
        let event = '';
        let data = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) event = line.slice(7).trim();
          else if (line.startsWith('data: ')) data = line.slice(6);
        }
        if (!event || !data) continue;

        try {
          const payload = JSON.parse(data);
          switch (event) {
            case 'meta':
              callbacks.onMeta(payload.status);
              break;
            case 'delta':
              callbacks.onDelta(payload.field, payload.text);
              break;
            case 'partial':
              if (payload.summary_bullets) callbacks.onBullets(payload.summary_bullets);
              break;
            case 'done':
              callbacks.onDone(payload);
              break;
            case 'error':
              callbacks.onError(payload.message);
              break;
          }
        } catch {
          // skip malformed frame
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export const reportsApi = {
  /** GET /api/reports/customer-sales */
  customerSales: (params) => getJson('/api/reports/customer-sales', params),

  /** GET /api/reports/contract-performance */
  contractPerformance: (params) =>
    getJson('/api/reports/contract-performance', params),

  /** SSE: GET /api/reports/contract-performance/ai-analysis */
  streamAiAnalysis: streamContractAiAnalysis,
};
