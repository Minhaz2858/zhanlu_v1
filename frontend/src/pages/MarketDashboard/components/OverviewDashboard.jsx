import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import html2canvas from 'html2canvas';
import { jsPDF } from 'jspdf';
import { Download, FileImage, FileText, Loader2 } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { ERP_PRODUCT_IDS, PRODUCT_LABELS } from '@/constants/marketDashboard';
import { getPriceChangeTextClass } from '../lib/priceChange';
import { TYPO } from '../lib/typography';
import { formatKpiValue as formatKpiValueBase } from '../lib/formatKpiValue';

/**
 * OverviewDashboard — Chemical Product Market Overview.
 *
 * Pixel-faithful port of EDIA_5.1.2/frontend/src/components/OverviewDashboard.tsx
 * (2,077 lines TSX) into zhanlu_7_30. Stripped of TypeScript, switched to
 * `marketDashboardApi` (uses `authFetch` so the user's JWT is attached), and
 * uses `PRODUCT_LABELS` for human-readable product names.
 *
 * Visual sections, in order:
 *   1. Left sidebar — Report Date + MARKET INDICATORS (Brent Crude / Naphtha / Cracked C5)
 *   2. Header — "Chemical Product Market Overview"
 *   3. PRICES OF CHEMICAL PRODUCTS — three gradient-headed cards (Upstream / Midstream / Downstream)
 *      with per-product regional price rows.
 *   4. 7-DAY AI PRICE OUTLOOK · EDIA FORECAST MODEL — per-group forecast cards.
 *   5. INDUSTRY INTELLIGENCE — three columns (Feedstock & Crude / Midstream / Downstream)
 *      with weekly impact + explanation box.
 *   6. Footer — sources note.
 *
 * The component follows the app's light/dark theme (html.dark selectors are
 * defined in `src/index.css` for the `.overview-*` classes).
 */

const OVERVIEW_ROW_GRID =
  'grid grid-cols-[minmax(0,1fr)_64px_112px_78px] items-center gap-2';

const GROUPS = [
  { key: 'upstream',   ids: ['crude_oil', 'naphtha', 'cracked_c5', 'cracked_c9'] },
  { key: 'midstream',  ids: ['isoprene', 'piperylene', 'dcpd', 'raffinate_c5'] },
  { key: 'downstream', ids: ['sis', 'c5_resin', 'blowing_agent', 'styrene'] },
];

// Truly flat: no colored fill anywhere — headers are white with a thin border,
// chips are muted gray. Direction is conveyed by label text + arrow, not color.
const GROUP_VISUALS = {
  upstream: {
    priceHeader: 'bg-card border-b border-border text-foreground',
    futuresHeader: 'bg-card border-b border-border text-foreground',
    chip: 'bg-muted text-muted-foreground border-border',
  },
  midstream: {
    priceHeader: 'bg-card border-b border-border text-foreground',
    futuresHeader: 'bg-card border-b border-border text-foreground',
    chip: 'bg-muted text-muted-foreground border-border',
  },
  downstream: {
    priceHeader: 'bg-card border-b border-border text-foreground',
    futuresHeader: 'bg-card border-b border-border text-foreground',
    chip: 'bg-muted text-muted-foreground border-border',
  },
};

const NEWS_LIMIT = 18;
const NEWS_COLUMN_LIMIT = 6;
const NEWS_WINDOW_HOURS = 168;
const OVERVIEW_SESSION_CACHE_TTL_MS = 30 * 60 * 1000;
const OVERVIEW_REGIONAL_LOAD_TIMEOUT_MS = 60_000;
const OVERVIEW_FORECAST_LOAD_TIMEOUT_MS = 60_000;
const REGIONAL_BATCH_CHUNK_SIZE = 3;
const OVERVIEW_SAFETY_REFRESH_MS = 30 * 60 * 1000;
const OVERVIEW_NEWS_CACHE_KEY = 'edia.overview.news.v1';
const OVERVIEW_FX_CACHE_KEY = 'edia.overview.fx.v1';

const RELATED_NEWS_TOKENS = [
  'c5', 'c9', 'naphtha', 'crude', 'isoprene', 'piperylene', 'dcpd', 'raffinate',
  'styrene', 'sis', 'resin', 'blowing',
  '石脑油', '原油', '裂解', '碳五', '碳九', '异戊二烯', '间戊二烯',
  '双环戊二烯', '抽余碳五', '苯乙烯', '树脂', '发泡剂',
];

const BAD_NEWS_PATTERNS = [
  'fallback extraction', 'source signal may affect', '请求存在威胁', '已被拦截',
  'waf', 'access denied', 'forbidden', 'captcha', 'robot', 'javascript enabled',
  'please enable javascript', "doesn't work properly without javascript",
  '验证', '拦截', '安全检测', '403', '404', '500',
];

const REGIONAL_LABEL_EN = {
  east_china: 'East China',
  south_china: 'South China',
  central_china: 'Central China',
  qingdao_f7: 'Qingdao Refining F7',
  wuhan: 'Central China (Wuhan)',
  average: 'National Avg.',
  yangzi: 'Yangzi',
  yangzi_basf: 'Yangzi BASF',
  sinokor: 'Sinokor',
  maoming: 'Maoming',
  guangzhou: 'Guangzhou',
  henhe_hc9: 'Henhe HC9',
  brent: 'Brent',
  japan_cif: 'Japan CIF',
  sinopec_sh: 'Sinopec Shanghai',
  baling_1105: 'Baling 1105',
  partner_forecast_price: 'Partner Forecast',
  juhui: 'Juhui',
  erp_sale_order_weighted: 'Co. Actual Avg.',
};

const REGIONAL_LABEL_ZH = {
  east_china: '华东',
  south_china: '华南',
  central_china: '华中',
  qingdao_f7: '青岛炼化 F7',
  wuhan: '华中（武汉）',
  average: '全国均价',
  yangzi: '扬子',
  yangzi_basf: '扬子巴斯夫',
  sinokor: '新湖',
  maoming: '茂名',
  guangzhou: '广州',
  henhe_hc9: '恒河 HC9',
  brent: '布伦特',
  japan_cif: '日本 CIF',
  sinopec_sh: '上海石化',
  baling_1105: '巴陵 1105',
  partner_forecast_price: '合作方预测',
  juhui: '聚辉',
  erp_sale_order_weighted: '公司实际均价',
};

const HIDDEN_REGIONAL_PRICE_TYPES = new Set(['erp_forecast_price']);

/* ── Text helpers ──────────────────────────────────────────────────── */

function normalizeText(raw) {
  return String(raw || '').replace(/\s+/g, ' ').trim();
}

function cleanNarrativeText(raw) {
  const text = normalizeText(raw);
  if (!text) return '';
  return text
    .replace(/^source key information\s*:\s*/i, '')
    .replace(/^来源关键信息\s*[:：]\s*/i, '')
    .trim();
}

function isLikelyUrl(value) {
  const text = normalizeText(value).toLowerCase();
  return (
    text.startsWith('http://') ||
    text.startsWith('https://') ||
    text.startsWith('www.')
  );
}

function compactSnippet(value, maxLen = 120) {
  const text = cleanNarrativeText(value);
  if (!text) return '';
  return text.length > maxLen ? `${text.slice(0, maxLen).trim()}...` : text;
}

function containsBadPattern(text) {
  const normalized = normalizeText(text).toLowerCase();
  if (!normalized) return false;
  return BAD_NEWS_PATTERNS.some((pattern) =>
    normalized.includes(pattern.toLowerCase()),
  );
}

/* ── News scoring & classification ────────────────────────────────── */

function mapCredibility(credibility) {
  if (typeof credibility === 'number') return credibility;
  const normalized = normalizeText(String(credibility || ''))
    .toLowerCase()
    .replace(/[_\s-]/g, '');
  if (!normalized) return 0;
  if (normalized.includes('tier1')) return 0.95;
  if (normalized.includes('tier2')) return 0.7;
  if (normalized.includes('tier3')) return 0.45;
  if (normalized.includes('socialmedia')) return 0.3;
  return 0;
}

function isWithinHours(isoText, hours) {
  if (!isoText) return false;
  const timestamp = new Date(isoText).getTime();
  if (!Number.isFinite(timestamp)) return false;
  const delta = Date.now() - timestamp;
  if (delta < 0) return true;
  return delta <= hours * 60 * 60 * 1000;
}

function qualityScore(item) {
  const usefulness = typeof item.usefulness_score === 'number' ? item.usefulness_score : 0;
  const credibility = mapCredibility(item.source_credibility);
  const relevance = typeof item.relevance_score === 'number' ? item.relevance_score : 0;
  const impact = typeof item.impact_magnitude === 'number' ? item.impact_magnitude : 0;
  const recencyBoost = isWithinHours(item.detected_at, 24)
    ? 2
    : isWithinHours(item.detected_at, 72)
      ? 0.5
      : 0;
  const certaintyBoost = ['high', 'confirmed', 'certain', '高'].some((token) =>
    normalizeText(item.certainty).toLowerCase().includes(token),
  )
    ? 1
    : 0;
  return usefulness * 2 + credibility * 1.5 + relevance + impact * 0.2 + certaintyBoost + recencyBoost;
}

function isRelatedNews(item) {
  const relevance = normalizeText(item.relevance_to_c5_c9).toLowerCase();
  if (
    relevance &&
    (relevance.includes('direct') ||
      relevance.includes('high') ||
      relevance.includes('相关') ||
      relevance.includes('直接'))
  ) {
    return true;
  }
  const affected = Array.isArray(item.affected_commodities)
    ? item.affected_commodities.join(' ')
    : '';
  const merged = [
    item.headline, item.causal_chain_hint, item.raw_quote, item.full_text, affected,
  ].map(normalizeText).join(' ').toLowerCase();
  return RELATED_NEWS_TOKENS.some((token) => merged.includes(token.toLowerCase()));
}

function isProfessionalNews(item) {
  const headline = normalizeText(item.headline);
  const summary = normalizeText(
    item.summary || item.causal_chain_hint || item.raw_quote || item.full_text || '',
  );
  if (!headline || headline.length < 8 || isLikelyUrl(headline)) return false;
  if (containsBadPattern(headline) || containsBadPattern(summary)) return false;
  const combined = `${headline} ${summary}`;
  if (!isRelatedNews(item)) return false;
  if (qualityScore(item) < 2) return false;
  return RELATED_NEWS_TOKENS.some((token) =>
    combined.toLowerCase().includes(token.toLowerCase()),
  );
}

function deriveSourceLabel(url) {
  const text = normalizeText(url);
  if (!text) return '';
  try {
    const normalized = text.startsWith('http://') || text.startsWith('https://')
      ? text
      : `https://${text}`;
    const hostname = new URL(normalized).hostname;
    return hostname || text;
  } catch {
    return text;
  }
}

function deriveHeadline(item) {
  const headline = normalizeText(item.headline);
  if (headline && !isLikelyUrl(headline)) return headline;
  const quote = compactSnippet(item.raw_quote, 100);
  if (quote && !isLikelyUrl(quote)) return quote;
  const hint = compactSnippet(item.causal_chain_hint, 100);
  if (hint && !isLikelyUrl(hint)) return hint;
  const sourceLabel = deriveSourceLabel(item.source_url);
  return sourceLabel || headline || 'Market event';
}

function deriveSummary(item, headline) {
  const candidates = [item.causal_chain_hint, item.raw_quote, item.full_text]
    .map((entry) => compactSnippet(entry, 160))
    .filter((entry) => entry && entry !== headline && !isLikelyUrl(entry));
  return candidates[0] || '';
}

function inferDirectionFromText(item) {
  const text = [
    item.headline, item.summary, item.causal_chain_hint, item.raw_quote, item.full_text,
  ].map(normalizeText).join(' ').toLowerCase();
  const upHints = [
    'bullish', 'rise', 'rising', 'increase', 'upward', 'outage', 'shutdown',
    'tight', 'shortage', 'tariff', 'sanction',
    '上涨', '上行', '偏强', '偏紧', '短缺', '检修', '停产', '限产', '加税', '制裁',
  ];
  const downHints = [
    'bearish', 'fall', 'falling', 'decrease', 'downward', 'oversupply',
    'weak demand', 'inventory build', 'price cut',
    '下跌', '下行', '偏弱', '过剩', '累库', '降价', '需求疲软', '需求走弱',
  ];
  const upScore = upHints.reduce((acc, token) => (text.includes(token) ? acc + 1 : acc), 0);
  const downScore = downHints.reduce((acc, token) => (text.includes(token) ? acc + 1 : acc), 0);
  if (upScore > downScore) return 'up';
  if (downScore > upScore) return 'down';
  return 'neutral';
}

function weeklyImpactSignal(item) {
  const explicitDirection = normalizeText(item.direction).toLowerCase();
  const direction = ['up', 'bullish', 'increase', 'rise', 'positive', '上涨', '上行'].includes(explicitDirection)
    ? 'up'
    : ['down', 'bearish', 'decrease', 'fall', 'negative', '下跌', '下行'].includes(explicitDirection)
      ? 'down'
      : inferDirectionFromText(item);

  const impact = typeof item.impact_magnitude === 'number' ? Math.max(0, Math.min(100, item.impact_magnitude)) : 0;
  const relevance = typeof item.relevance_score === 'number' ? Math.max(0, Math.min(10, item.relevance_score)) : 0;
  const usefulness = typeof item.usefulness_score === 'number' ? Math.max(0, Math.min(10, item.usefulness_score)) : 0;
  const credibility = Math.max(0, Math.min(1, mapCredibility(item.source_credibility)));
  const recencyBoost = isWithinHours(item.detected_at, 24)
    ? 10
    : isWithinHours(item.detected_at, 72)
      ? 5
      : 0;
  const certainty = normalizeText(item.certainty).toLowerCase();
  const certaintyBoost = ['high', 'confirmed', 'certain', '高', '确认'].some((t) => certainty.includes(t))
    ? 10
    : ['medium', '中'].some((t) => certainty.includes(t))
      ? 5
      : 0;

  const scoreRaw =
    impact * 0.35 + relevance * 4 + usefulness * 2 + credibility * 20 + recencyBoost + certaintyBoost;
  const score = Math.max(0, Math.min(100, Math.round(scoreRaw)));
  const confidence = score >= 75 ? 'high' : score >= 45 ? 'medium' : 'low';
  const rationale =
    compactSnippet(item.causal_chain_hint || item.summary || item.raw_quote || item.full_text, 110) || '--';
  return { direction, score, confidence, rationale };
}

// Monochromatic chip: direction is conveyed by the label text (Next week:
// Upward / Downward / Neutral pressure) plus the arrow glyph, not by color.
function weeklyImpactTone(_direction) {
  return 'text-foreground bg-secondary border-border';
}

function weeklyDirectionLabel(direction, language) {
  if (language === 'zh') {
    if (direction === 'up') return '下周偏强';
    if (direction === 'down') return '下周偏弱';
    return '下周中性';
  }
  if (direction === 'up') return 'Next week: Upward pressure';
  if (direction === 'down') return 'Next week: Downward pressure';
  return 'Next week: Neutral';
}

function weeklyConfidenceLabel(confidence, language) {
  if (language === 'zh') {
    if (confidence === 'high') return '高置信';
    if (confidence === 'medium') return '中置信';
    return '低置信';
  }
  if (confidence === 'high') return 'High confidence';
  if (confidence === 'medium') return 'Medium confidence';
  return 'Low confidence';
}

function buildDirectNewsExplanation(item, language) {
  const impact = weeklyImpactSignal(item);
  const detail = compactSnippet(
    item.summary || item.causal_chain_hint || item.raw_quote || item.full_text,
    150,
  );
  const dirLabel = weeklyDirectionLabel(impact.direction, language);
  const confLabel = weeklyConfidenceLabel(impact.confidence, language);
  if (language === 'zh') {
    const core = detail || '该信息与产业链供需和情绪有关，需结合价格与库存跟踪确认。';
    return `判断：${dirLabel}（影响 ${impact.score}/100，${confLabel}）。${core}`;
  }
  const core =
    detail || 'This signal may affect chain supply-demand and sentiment; validate with price and inventory moves.';
  return `View: ${dirLabel} (impact ${impact.score}/100, ${confLabel}). ${core}`;
}

function sortByNewsPriority(items) {
  return [...items].sort((left, right) => {
    const leftDaily = isWithinHours(left.detected_at, 24) ? 1 : 0;
    const rightDaily = isWithinHours(right.detected_at, 24) ? 1 : 0;
    if (leftDaily !== rightDaily) return rightDaily - leftDaily;
    const impactScoreDiff = weeklyImpactSignal(right).score - weeklyImpactSignal(left).score;
    if (impactScoreDiff !== 0) return impactScoreDiff;
    const scoreDiff = qualityScore(right) - qualityScore(left);
    if (scoreDiff !== 0) return scoreDiff;
    return String(right.detected_at || '').localeCompare(String(left.detected_at || ''));
  });
}

function dedupeByHeadline(items) {
  const seen = new Set();
  const result = [];
  for (const item of items) {
    const key = normalizeText(item.headline).toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    result.push(item);
  }
  return result;
}

function normalizeEvent(item) {
  const headline = deriveHeadline(item);
  return {
    ...item,
    headline,
    summary: deriveSummary(item, headline),
    source_label: deriveSourceLabel(item.source_url),
  };
}

function selectUsefulNews(items) {
  const highQuality = items.filter(isProfessionalNews);
  const relatedFallback = items.filter((item) => {
    const headline = normalizeText(item.headline);
    const summary = normalizeText(item.summary);
    return isRelatedNews(item) && !containsBadPattern(headline) && !containsBadPattern(summary);
  });
  const weeklyRelated = relatedFallback.filter((item) =>
    isWithinHours(item.detected_at, NEWS_WINDOW_HOURS),
  );
  const highQualityIds = new Set(
    highQuality
      .filter((item) => isWithinHours(item.detected_at, NEWS_WINDOW_HOURS))
      .map((item) => item.event_id),
  );
  const effectiveWeekly = weeklyRelated.filter(
    (item) =>
      highQualityIds.has(item.event_id) ||
      isWithinHours(item.detected_at, 24) ||
      weeklyImpactSignal(item).score >= 20 ||
      qualityScore(item) >= 2,
  );
  const base = effectiveWeekly.length > 0 ? effectiveWeekly : weeklyRelated;
  return dedupeByHeadline(sortByNewsPriority(base)).slice(0, NEWS_LIMIT);
}

function classifyNewsColumn(item) {
  const eventType = normalizeText(item.event_type).toLowerCase();
  const sourceUrl = normalizeText(item.source_url).toLowerCase();
  const affected = Array.isArray(item.affected_commodities)
    ? item.affected_commodities.join(' ').toLowerCase()
    : '';
  const combined = [
    item.headline, item.summary, item.causal_chain_hint, item.raw_quote,
    item.full_text, affected,
  ].map(normalizeText).join(' ').toLowerCase();

  if (eventType.includes('demand')) {
    if (sourceUrl.includes('ndrc.gov.cn') || sourceUrl.includes('cpcia.org.cn') ||
        combined.includes('政策') || combined.includes('需求') || combined.includes('market')) {
      return 'downstream';
    }
    if (sourceUrl.includes('oilchem.net') || sourceUrl.includes('sci99.com')) {
      return 'midstream';
    }
  }
  if (eventType.includes('opec') || eventType.includes('geopolitical') ||
      eventType.includes('shipping') || eventType.includes('sanction') ||
      /\b(crude|brent|wti|naphtha)\b/.test(combined) ||
      combined.includes('原油') || combined.includes('石脑油') || combined.includes('欧佩克')) {
    return 'upstream';
  }
  if (eventType.includes('sinopec') || eventType.includes('plant') ||
      /\b(c5|c9|isoprene|piperylene|dcpd|sis|styrene|resin)\b/.test(combined) ||
      combined.includes('碳五') || combined.includes('碳九') ||
      combined.includes('异戊二烯') || combined.includes('间戊二烯') ||
      combined.includes('双环戊二烯') || combined.includes('苯乙烯') ||
      combined.includes('树脂')) {
    return 'midstream';
  }
  return 'downstream';
}

/* ── Date / label formatting ──────────────────────────────────────── */

function formatDateLabel(language, locale) {
  const now = new Date();
  if (language === 'zh') {
    return now.toLocaleDateString('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'long',
    });
  }
  return now.toLocaleDateString(locale, {
    year: 'numeric', month: 'short', day: '2-digit', weekday: 'long',
  });
}

function formatNewsTimestamp(value, language, locale) {
  if (!value) return '--';
  const d = new Date(value);
  if (!Number.isFinite(d.getTime())) return value;
  if (language === 'zh') {
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleString(locale, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}

/* ── Session / local storage cache helpers ────────────────────────── */

function readOverviewSessionCache(key, maxAgeMs = OVERVIEW_SESSION_CACHE_TTL_MS, options) {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.cachedAt !== 'number') return null;
    if (!options?.allowStale && Date.now() - parsed.cachedAt > maxAgeMs) {
      return null;
    }
    return parsed.value ?? null;
  } catch {
    return null;
  }
}

function writeOverviewSessionCache(key, value) {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(key, JSON.stringify({ cachedAt: Date.now(), value }));
  } catch {
    /* ignore quota */
  }
}

function readRegionalOverviewCache(key) {
  const cached = readOverviewSessionCache(key);
  if (!cached || Object.keys(cached).length === 0) return null;
  return cached;
}

function writeRegionalOverviewCache(key, value) {
  if (Object.keys(value).length === 0) return;
  writeOverviewSessionCache(key, value);
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, JSON.stringify({ cachedAt: Date.now(), value }));
  } catch {
    /* ignore quota */
  }
}

function readForecastOverviewCache(key) {
  const fromSession = readOverviewSessionCache(key);
  if (fromSession) return fromSession;
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.cachedAt !== 'number') return null;
    if (Date.now() - parsed.cachedAt > OVERVIEW_SESSION_CACHE_TTL_MS) return null;
    const value = parsed.value ?? null;
    if (!value || Object.keys(value).length === 0) return null;
    return value;
  } catch {
    return null;
  }
}

function writeForecastOverviewCache(key, value) {
  if (Object.keys(value).length === 0) return;
  writeOverviewSessionCache(key, value);
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, JSON.stringify({ cachedAt: Date.now(), value }));
  } catch {
    /* ignore quota */
  }
}

function clearOverviewSessionCachePrefix(prefix) {
  if (typeof window === 'undefined') return;
  const keysToDelete = [];
  for (let i = 0; i < window.sessionStorage.length; i += 1) {
    const k = window.sessionStorage.key(i);
    if (k?.startsWith(prefix)) keysToDelete.push(k);
  }
  keysToDelete.forEach((k) => window.sessionStorage.removeItem(k));
}

function chunkArray(items, size) {
  const chunks = [];
  for (let i = 0; i < items.length; i += size) {
    chunks.push(items.slice(i, i + size));
  }
  return chunks;
}

/* ── Display-name helper ──────────────────────────────────────────── */

function defaultDisplayName(id, fallback, language = 'en') {
  const labels = PRODUCT_LABELS[id];
  if (!labels) return fallback || id;
  if (language === 'zh') return labels.label_zh || labels.label_en || fallback || id;
  return labels.label_en || labels.label_zh || fallback || id;
}

/* ── Component ────────────────────────────────────────────────────── */

export default function OverviewDashboard({
  snapshot = [],
  language = 'en',
  locale,
  marketRefreshToken = 0,
  eventsRefreshToken = 0,
  displayName,
}) {
  const resolvedLocale = locale || (typeof navigator !== 'undefined' ? navigator.language : 'en-US') || 'en-US';
  const noData = language === 'zh' ? '暂无数据' : 'No Data Available';
  const resolveName = displayName || ((id, fb) => defaultDisplayName(id, fb, language));
  const queryClient = useQueryClient();

  const text = language === 'en'
    ? {
        reportDate: 'Report Date',
        marketIndicators: 'Market Indicators',
        brent: 'Brent Crude Oil',
        naphthaLabel: 'Japanese Naphtha CFR',
        crackedC5Label: 'Cracked C5',
        unavailable: 'Feed unavailable',
        overviewTitle: 'Chemical Product Market Overview',
        prices: 'Prices of Chemical Products',
        product: 'Product',
        date: 'Date',
        price: 'Price',
        dailyMove: 'Daily',
        loading: 'Loading...',
        regionalUnavailable: 'No regional data',
        supplyChainKpiTitle: 'Supply Chain KPIs',
        grade: 'Grade',
        sku: 'SKU',
        sales30d: '30D Sales',
        inventory: 'Inventory',
        inventoryDays: 'Days',
        pending: 'Pending',
        noGradeData: 'No data',
        execution: 'Execution',
        futuresWeeklyGapTitle: '7-Day AI Price Outlook · EDIA Forecast Model',
        forecast7dLabel: '7-day forecast',
        forecastLabel: 'Forecast',
        items: 'items',
        industryIntelligenceTitle: 'Industry Intelligence',
        feedstockAndCrude: 'Feedstock & Crude',
        midstreamProducts: 'Midstream Products',
        downstreamAndMarket: 'Downstream & Market',
        openLink: 'Open',
        noNews: 'No recent industry events found.',
        sourcesNote: 'Sources: Longzhong · SCI · Sinopec · ERP · Internal use only · All prices RMB/mt unless noted',
        exportTitle: 'Export',
        exportPng: 'Export as PNG',
        exportPdf: 'Export as PDF',
        exporting: 'Generating…',
        exportFailed: 'Export failed. Please try again.',
      }
    : {
        reportDate: '报告日期',
        marketIndicators: '市场指标',
        brent: '布伦特原油',
        naphthaLabel: '日本石脑油 CFR',
        crackedC5Label: '裂解碳五',
        unavailable: '暂无实时数据',
        overviewTitle: '化工产品市场总览',
        prices: '化工产品价格',
        product: '产品',
        date: '日期',
        price: '价格',
        dailyMove: '日涨跌',
        loading: '加载中...',
        regionalUnavailable: '暂无区域数据',
        supplyChainKpiTitle: '供应链KPI',
        grade: '牌号',
        sku: 'SKU',
        sales30d: '30日销量',
        inventory: '库存',
        inventoryDays: '库存天数',
        pending: '待交量',
        noGradeData: '暂无数据',
        execution: '执行',
        futuresWeeklyGapTitle: '7日AI价格展望 · EDIA预测模型',
        forecast7dLabel: '7日预测',
        forecastLabel: '预测',
        items: '条',
        industryIntelligenceTitle: '行业资讯动态',
        feedstockAndCrude: '石化要闻',
        midstreamProducts: '基础化工',
        downstreamAndMarket: '行业动态',
        openLink: '查看',
        noNews: '暂无近期行业事件。',
        sourcesNote: '来源：隆众 · SCI · 中石化 · ERP · 仅供内部使用 · 除特别说明外单位为人民币/吨',
        exportTitle: '导出',
        exportPng: '导出为 PNG',
        exportPdf: '导出为 PDF',
        exporting: '正在生成…',
        exportFailed: '导出失败，请重试。',
      };

  /* ── News (events) — caching/refresh handled inside component ─── */

  const [news, setNews] = useState([]);
  const [newsLoading, setNewsLoading] = useState(false);
  const [newsError, setNewsError] = useState(null);
  const [newsUpdatedAt, setNewsUpdatedAt] = useState('');

  /* ── Export (PNG / PDF) ─────────────────────────────────────────── */

  const exportRef = useRef(null);
  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  const [exportingFormat, setExportingFormat] = useState(null);
  const exportMenuRef = useRef(null);

  // Close the dropdown on outside click / Escape.
  useEffect(() => {
    if (!exportMenuOpen) return undefined;
    const onPointerDown = (event) => {
      if (exportMenuRef.current && !exportMenuRef.current.contains(event.target)) {
        setExportMenuOpen(false);
      }
    };
    const onKeyDown = (event) => {
      if (event.key === 'Escape') setExportMenuOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [exportMenuOpen]);

  const handleExport = async (format) => {
    if (!exportRef.current || exportingFormat) return;
    setExportMenuOpen(false);
    setExportingFormat(format);
    try {
      // Give React a tick to close the dropdown before capturing.
      await new Promise((resolve) => setTimeout(resolve, 50));
      // Force the desktop 3-column export layout (sidebar + 3 columns),
      // then remove the class once the canvas is captured.
      exportRef.current.classList.add('overview-export-mode');
      await new Promise((resolve) => setTimeout(resolve, 80));
      const canvas = await html2canvas(exportRef.current, {
        scale: 2,
        useCORS: true,
        backgroundColor: '#ffffff',
        logging: false,
        windowWidth: 1440,
      });
      const dateStamp = new Date().toISOString().slice(0, 10);
      const baseName = `chemical-market-overview-${dateStamp}`;
      if (format === 'png') {
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
        if (!blob) throw new Error('canvas toBlob returned null');
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `${baseName}.png`;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10_000);
      } else {
        const pdf = new jsPDF({
          orientation: 'landscape',
          unit: 'mm',
          format: 'a4',
          compress: true,
        });
        const pageWidth = pdf.internal.pageSize.getWidth();
        const pageHeight = pdf.internal.pageSize.getHeight();
        const imgWidth = pageWidth;
        const imgHeight = (canvas.height * imgWidth) / canvas.width;
        const totalPages = Math.ceil(imgHeight / pageHeight);
        for (let pageIndex = 0; pageIndex < totalPages; pageIndex += 1) {
          if (pageIndex > 0) pdf.addPage();
          const yOffset = -pageHeight * pageIndex;
          pdf.addImage(canvas.toDataURL('image/jpeg', 0.92), 'JPEG', 0, yOffset, imgWidth, imgHeight, undefined, 'FAST');
        }
        pdf.save(`${baseName}.pdf`);
      }
    } catch (error) {
      console.error('Overview export failed:', error);
      window.alert(text.exportFailed);
    } finally {
      if (exportRef.current) exportRef.current.classList.remove('overview-export-mode');
      setExportingFormat(null);
    }
  };

  const loadNews = async (showLoading) => {
    if (showLoading) setNewsLoading(true);
    setNewsError(null);
    try {
      const payload = await marketDashboardApi.events(NEWS_WINDOW_HOURS, 'approved', 0);
      const weeklyEvents = Array.isArray(payload.events) ? payload.events : [];
      const selected = selectUsefulNews(weeklyEvents.map(normalizeEvent));
      const updatedAt = new Date().toISOString();
      setNews(selected);
      setNewsUpdatedAt(updatedAt);
      writeOverviewSessionCache(OVERVIEW_NEWS_CACHE_KEY, { news: selected, updatedAt });
    } catch (err) {
      const msg = err?.message || 'Failed to load events';
      if (!/abort/i.test(msg)) setNewsError(msg);
      if (showLoading) setNews([]);
    } finally {
      if (showLoading) setNewsLoading(false);
    }
  };

  useEffect(() => {
    const cached = readOverviewSessionCache(OVERVIEW_NEWS_CACHE_KEY);
    if (cached) {
      setNews(cached.news || []);
      setNewsUpdatedAt(cached.updatedAt || '');
    }
    void loadNews(!cached);

    const timerId = setInterval(() => {
      if (document.visibilityState === 'visible') void loadNews(false);
    }, OVERVIEW_SAFETY_REFRESH_MS);
    return () => clearInterval(timerId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!eventsRefreshToken) return;
    if (typeof window !== 'undefined') {
      try { window.sessionStorage.removeItem(OVERVIEW_NEWS_CACHE_KEY); } catch { /* ignore */ }
    }
    void loadNews(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventsRefreshToken]);

  /* ── One-shot overview bundle (snapshot + regional + forecast + fx) ──
   * Single round-trip; 30s backend in-memory cache makes repeated views
   * take ~15ms. SessionStorage caches are kept only as offline fallback.
   * `?noCache=1` query param forces a fresh build. */

  const overviewQuery = useQuery({
    queryKey: ['market-dashboard', 'overview-bundle'],
    queryFn: () => marketDashboardApi.overview({ noCache: false, forecastDay: 7 }),
    staleTime: 25_000,  // slightly under backend TTL to keep one source of truth
    gcTime: 5 * 60_000,
    retry: 1,
  });

  const bundle = overviewQuery.data || null;
  const bundleReady = !!bundle?.snapshot;

  // Pull typed subsections out of the bundle
  const fxRate = bundle?.fx || null;
  const hasFxRate = fxRate && fxRate.available !== false && fxRate.price != null && fxRate.as_of_date;
  const usdrmb = hasFxRate ? fxRate.price : null;

  const weeklyForecastBaseByProduct = bundle?.forecast_base_by_product || {};
  const forecastOverlayByProduct = bundle?.forecast_overlay_by_product || {}; // Phase 6
  const gradeKpisByProduct = bundle?.grade_kpis_by_product || {};

  /* Regional row payload by product_id. The bundle returns the schema used
   * by the regional-batch endpoint: { items: { product_id: { regions: [...] } } }
   * We flatten to a per-product map of regional data. */
  const regionalBundleItems = bundle?.regional || {};

  // Prefer the bundle snapshot if it has rows (faster path); fall back to
  // the prop for backward-compat with callers that already pass snapshot.
  const sourceSnapshot = (bundle?.snapshot && bundle.snapshot.length > 0)
    ? bundle.snapshot
    : snapshot;
  const snapshotById = useMemo(
    () => new Map(sourceSnapshot.map((item) => [item.id, item])),
    [sourceSnapshot],
  );

  const groupedRows = useMemo(
    () => GROUPS.map((group) => ({
      key: group.key,
      rows: group.ids
        .map((id) => snapshotById.get(id))
        .filter(Boolean),
    })),
    [snapshotById],
  );

  const regionalProductIds = useMemo(
    () => Array.from(
      new Set(groupedRows.flatMap((g) => g.rows.map((r) => r.id))),
    ).sort(),
    [groupedRows],
  );
  const regionalProductIdsKey = regionalProductIds.join(',');
  const regionalCacheKey = `edia.overview.regional.v1:${regionalProductIdsKey}`;
  const forecastCacheKey = `edia.overview.forecast.v1:${regionalProductIdsKey}`;

  // Persist regional+forecast as offline fallback (alongside bundle)
  useEffect(() => {
    if (Object.keys(regionalBundleItems).length > 0) {
      writeRegionalOverviewCache(regionalCacheKey, regionalBundleItems);
    }
  }, [regionalBundleItems, regionalCacheKey]);

  useEffect(() => {
    if (Object.keys(weeklyForecastBaseByProduct).length > 0) {
      writeForecastOverviewCache(forecastCacheKey, weeklyForecastBaseByProduct);
    }
  }, [weeklyForecastBaseByProduct, forecastCacheKey]);

  // Discard any stale sessionStorage cache from older schema-bug revisions
  // (kept the keys, but force-refresh once on mount).
  const clearedStaleSessionRef = useRef(false);
  useEffect(() => {
    if (clearedStaleSessionRef.current) return;
    clearedStaleSessionRef.current = true;
    try {
      // Drop regional/forecast session caches so the new bundle payload
      // isn't compared to a stale "0 regions" cache when offline.
      for (const k of Object.keys(window.sessionStorage)) {
        if (
          k.startsWith('edia.overview.regional.v1:') ||
          k.startsWith('edia.overview.forecast.v1:')
        ) {
          window.sessionStorage.removeItem(k);
        }
      }
    } catch { /* ignore */ }
  }, []);

  /* ── Sidebar snapshot rows (Brent / Naphtha / Cracked C5) ─────── */

  const brent   = snapshotById.get('crude_oil') ?? null;
  const naphtha = snapshotById.get('naphtha') ?? null;
  const crackedC5 = snapshotById.get('cracked_c5') ?? null;

  /* ── Number / formatting helpers ──────────────────────────────── */

  const toAbsoluteChange = (latestPrice, pct) => {
    if (latestPrice == null || pct == null) return null;
    const denominator = 100 + pct;
    if (denominator === 0) return null;
    return latestPrice * (pct / denominator);
  };

  const resolveGap = (latestPrice, pct, explicitGap) => {
    if (typeof explicitGap === 'number' && Number.isFinite(explicitGap)) return explicitGap;
    return toAbsoluteChange(latestPrice, pct);
  };

  const formatAbsoluteChange = (latestPrice, pct, explicitGap) => {
    const value = resolveGap(latestPrice, pct, explicitGap);
    if (value == null || !Number.isFinite(value)) return noData;
    const abs = Math.abs(value);
    const formatted = abs.toLocaleString(resolvedLocale, {
      minimumFractionDigits: abs < 10 ? 2 : 0,
      maximumFractionDigits: abs < 10 ? 2 : 0,
    });
    return `${value > 0 ? '+' : value < 0 ? '-' : ''}${formatted}`;
  };

  const changeSignal = (latestPrice, pct, explicitGap) => {
    const value = resolveGap(latestPrice, pct, explicitGap);
    if (value == null || !Number.isFinite(value)) return pct ?? null;
    return value;
  };

  const formatPriceValue = (value) => {
    if (value == null || !Number.isFinite(value)) return noData;
    return value.toLocaleString(resolvedLocale, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  };

  const formatKpiValue = (value) => formatKpiValueBase(value, resolvedLocale);

  const changeColor = (value, neutral = 'text-slate-500') => {
    if (value == null) return neutral;
    return getPriceChangeTextClass(value);
  };

  const todayIsoDate = useMemo(() => {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    return `${now.getFullYear()}-${month}-${day}`;
  }, []);

  const formatShortDate = (dateValue) => {
    const normalized = String(dateValue || '').slice(0, 10);
    return normalized ? normalized.slice(5) : '--';
  };

  const dateTextClass = (dateValue) => (dateValue ? 'text-slate-800 dark:text-slate-200' : 'text-slate-400');

  /* ── News columns (memoized) ─────────────────────────────────── */

  const newsColumns = useMemo(() => {
    const columns = { upstream: [], midstream: [], downstream: [] };
    const overflow = [];
    for (const item of news) {
      const columnKey = classifyNewsColumn(item);
      if (columns[columnKey].length < NEWS_COLUMN_LIMIT) {
        columns[columnKey].push(item);
      } else {
        overflow.push(item);
      }
    }
    const targets = ['upstream', 'midstream', 'downstream'];
    for (const item of overflow) {
      const target = targets
        .filter((k) => columns[k].length < NEWS_COLUMN_LIMIT)
        .sort((l, r) => columns[l].length - columns[r].length)[0];
      if (!target) break;
      columns[target].push(item);
    }
    return [columns.upstream, columns.midstream, columns.downstream];
  }, [news]);

  const forecastLoading = overviewQuery.isLoading && !bundle;

  /* ── Render ──────────────────────────────────────────────────── */

  return (
    <div className="h-full overflow-y-auto bg-background">
      <div ref={exportRef} className="flex min-h-full">
        <aside className="hidden xl:flex w-[168px] shrink-0 flex-col border-r border-border bg-secondary px-3.5 py-5 text-foreground sticky top-0 self-start max-h-screen">
          <div className="rounded-lg border border-border bg-card px-3 py-2.5">
            <p className={TYPO.micro}>{text.reportDate}</p>
            <p className="mt-1 text-[16px] font-semibold text-foreground">
              {formatDateLabel(language, resolvedLocale)}
            </p>
          </div>

          <p className={'mt-4 mb-2 ' + TYPO.micro + ' uppercase tracking-[0.1em] text-muted-foreground'}>
            {text.marketIndicators}
          </p>

          <div className="space-y-1.5">
            <div className="rounded-md border border-border bg-card px-2.5 py-2">
              <p className="text-[9px] md:text-[10px] text-muted-foreground">{text.brent}</p>
              <p className="font-mono text-[15px] font-semibold text-foreground">
                {brent ? brent.price.toLocaleString(resolvedLocale) : noData}
              </p>
              <p className={`font-mono text-[11px] md:text-[12px] ${changeColor(changeSignal(brent?.price, brent?.change_pct), 'text-muted-foreground')}`}>
                {brent
                  ? formatAbsoluteChange(brent.price, brent.change_pct, brent.daily_gap_value)
                  : noData}
              </p>
            </div>

            <div className="rounded-md border border-border bg-card px-2.5 py-2">
              <p className="text-[9px] md:text-[10px] text-muted-foreground">{text.naphthaLabel}</p>
              <p className="font-mono text-[15px] font-semibold text-foreground">
                {naphtha ? naphtha.price.toLocaleString(resolvedLocale) : noData}
              </p>
              <p className={`font-mono text-[11px] md:text-[12px] ${changeColor(changeSignal(naphtha?.price, naphtha?.change_pct), 'text-muted-foreground')}`}>
                {naphtha
                  ? formatAbsoluteChange(naphtha.price, naphtha.change_pct, naphtha.daily_gap_value)
                  : noData}
              </p>
            </div>

            <div className="rounded-md border border-border bg-card px-2.5 py-2">
              <p className="text-[9px] md:text-[10px] text-muted-foreground">{text.crackedC5Label}</p>
              <p className="font-mono text-[15px] font-semibold text-foreground">
                {crackedC5 ? crackedC5.price.toLocaleString(resolvedLocale) : noData}
              </p>
              <p className={`font-mono text-[11px] md:text-[12px] ${changeColor(changeSignal(crackedC5?.price, crackedC5?.change_pct), 'text-muted-foreground')}`}>
                {crackedC5
                  ? formatAbsoluteChange(crackedC5.price, crackedC5.change_pct, crackedC5.daily_gap_value)
                  : noData}
              </p>
            </div>
          </div>
        </aside>

        <main className="min-w-0 flex-1 px-3 py-4 md:px-4 lg:px-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 dark:border-slate-700 pb-3">
            <h2 className="text-[15px] md:text-[16px] font-semibold text-slate-800 dark:text-slate-100">
              {text.overviewTitle}
            </h2>
            <div className="flex items-center gap-3 text-[10px] md:text-[11px] text-slate-400 dark:text-slate-500">
              {bundleReady && (
                <span title={bundle.cached ? '30s in-memory backend cache hit' : 'fresh from EDIA warehouse'}>
                  {bundle.cached
                    ? (language === 'zh' ? `缓存 ${Math.round((bundle.cache_age_ms || 0) / 1000)}s` : `cached ${Math.round((bundle.cache_age_ms || 0) / 1000)}s`)
                    : (language === 'zh' ? '实时' : 'live')}
                </span>
              )}
              <div ref={exportMenuRef} className="relative">
                <button
                  type="button"
                  aria-label={text.exportTitle}
                  aria-haspopup="menu"
                  aria-expanded={exportMenuOpen}
                  disabled={Boolean(exportingFormat)}
                  onClick={() => setExportMenuOpen((open) => !open)}
                  className="inline-flex items-center gap-1 rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700 disabled:opacity-60"
                >
                  {exportingFormat ? (
                    <Loader2 width="11" height="11" className="animate-spin" />
                  ) : (
                    <Download width="11" height="11" />
                  )}
                  {exportingFormat ? text.exporting : text.exportTitle}
                </button>
                {exportMenuOpen && !exportingFormat && (
                  <div
                    role="menu"
                    className="absolute right-0 top-full z-50 mt-1 w-44 overflow-hidden rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-lg"
                  >
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => handleExport('png')}
                      className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700"
                    >
                      <FileImage width="13" height="13" className="shrink-0 text-slate-400" />
                      {text.exportPng}
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => handleExport('pdf')}
                      className="flex w-full items-center gap-2 border-t border-slate-100 dark:border-slate-700 px-3 py-2 text-left text-[12px] text-slate-700 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-700"
                    >
                      <FileText width="13" height="13" className="shrink-0 text-slate-400" />
                      {text.exportPdf}
                    </button>
                  </div>
                )}
              </div>
              <button
                type="button"
                aria-label="Refresh overview"
                disabled={overviewQuery.isFetching}
                onClick={async () => {
                  try {
                    // Drop the backend in-memory cache so the next /overview
                    // call rebuilds from MySQL.
                    await marketDashboardApi.refreshOverview();
                  } catch (e) { /* ignore cache-clear failure */ }
                  // Force a no-cache fetch and update the cached page data.
                  try {
                    const fresh = await marketDashboardApi.overview({ noCache: true, forecastDay: 7 });
                    queryClient.setQueryData(['market-dashboard', 'overview-bundle'], fresh);
                  } catch (e) {
                    // Fall through to the regular invalidation flow.
                    queryClient.invalidateQueries({ queryKey: ['market-dashboard', 'overview-bundle'] });
                  }
                }}
                className="inline-flex items-center gap-1 rounded border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-2 py-1 font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700 disabled:opacity-60"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className={overviewQuery.isFetching ? 'animate-spin' : ''}>
                  <polyline points="23 4 23 10 17 10"></polyline>
                  <polyline points="1 20 1 14 7 14"></polyline>
                  <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
                </svg>
                {language === 'zh' ? '刷新' : 'Refresh'}
              </button>
            </div>
          </div>

          {/* Initial-loading placeholder (replaces the whole data tree when
              no bundle has arrived yet, so users don't see "0 products"
              while we're hitting EDIA for the first time). */}
          {!bundleReady && (
            <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 text-[13px] text-slate-500">
              {language === 'zh' ? '正在从EDIA数据库拉取实时数据...' : 'Fetching live data from EDIA database…'}
            </div>
          )}

          {/* PRICES section */}
          <section className="mb-6">
            <h3 className="mb-3 flex items-center gap-2 text-[11px] md:text-[12px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              <span className="block h-3 w-[3px] rounded bg-muted-foreground" />
              {text.prices}
            </h3>

            <div data-overview-grid="3col" className="grid grid-cols-1 gap-2.5 lg:grid-cols-3">
              {groupedRows.map((group) => (
                <article key={group.key} className="overflow-hidden rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900">
                  <div className={`${GROUP_VISUALS[group.key].priceHeader} flex items-center justify-between px-3.5 py-2`}>
                    <h4 className="text-[22px] font-semibold">
                      {group.key === 'upstream' ? (language === 'zh' ? '上游' : 'Upstream')
                        : group.key === 'midstream' ? (language === 'zh' ? '中游' : 'Midstream')
                        : (language === 'zh' ? '下游' : 'Downstream')}
                    </h4>
                    <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] md:text-[12px] font-medium text-muted-foreground">
                      {group.rows.length}
                    </span>
                  </div>

                  <div className="grid grid-cols-[minmax(0,1fr)_64px_112px_78px] gap-2 border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/60 px-3.5 py-1.5 text-[10px] md:text-[11px] font-semibold uppercase tracking-[0.04em] text-slate-400 dark:text-slate-500">
                    <span>{text.product}</span>
                    <span className="text-right">{text.date}</span>
                    <span className="text-right">{text.price}</span>
                    <span className="text-right">{text.dailyMove}</span>
                  </div>

                  <div>
                    {group.rows.map((item) => {
                      const regional = regionalBundleItems[item.id];
                      const isLoadingThis = overviewQuery.isLoading && !regional;
                      return (
                        <div key={item.id} className="overview-product-unit product-unit border-b border-slate-200 dark:border-slate-700 last:border-b-0">
                          <div className="overview-product-label px-3.5 py-2 border-b border-slate-200 dark:border-slate-700">
                            <span className="overview-exec-primary block min-w-0 break-words text-[14px] md:text-[15px] font-semibold">
                              {resolveName(item.id, item.name)}
                            </span>
                          </div>

                          <div className="overview-regional-panel border-t border-slate-200 dark:border-slate-700">
                            {item.execution_price != null && (
                              <div className={`overview-regional-row ${OVERVIEW_ROW_GRID} border-b border-slate-200 dark:border-slate-700 px-3.5 py-1.5 text-[12px] md:text-[13px] overview-regional-internal`}>
                                <span className="min-w-0 truncate font-semibold text-foreground" title={language === 'en' ? 'Latest actual transaction price recorded in the system' : '系统中记录的最新实际成交价格'}>
                                  {text.execution}
                                </span>
                                <span className={`overview-exec-muted text-right text-[11px] md:text-[12px] ${dateTextClass(item.execution_price_date)}`} title={language === 'en' ? 'Date of this execution price' : '该成交价格的日期'}>
                                  {formatShortDate(item.execution_price_date)}
                                </span>
                                <span className="overview-exec-value text-right font-mono text-[13px] md:text-[14px]" title={language === 'en' ? `Execution price: ¥${item.execution_price.toLocaleString(resolvedLocale)}/ton` : `成交价：¥${item.execution_price.toLocaleString(resolvedLocale)}/吨`}>
                                  ¥{item.execution_price.toLocaleString(resolvedLocale)}
                                </span>
                                <span className={`overview-exec-value text-right font-mono text-[12px] md:text-[13px] ${changeColor(item.execution_price_change, 'text-slate-400')}`} title={
                                  language === 'en'
                                    ? item.execution_price_change != null
                                      ? `Change vs previous execution price (${formatShortDate(item.execution_price_prev_date) || 'previous'}): ${item.execution_price_change > 0 ? '+' : ''}¥${item.execution_price_change.toLocaleString(resolvedLocale)}/ton`
                                      : 'Change from previous execution price'
                                    : item.execution_price_change != null
                                      ? `较上笔成交价（${formatShortDate(item.execution_price_prev_date) || '前次'}）变动：${item.execution_price_change > 0 ? '+' : ''}¥${item.execution_price_change.toLocaleString(resolvedLocale)}/吨`
                                      : '较上笔成交价的变动'
                                }>
                                  {formatAbsoluteChange(item.execution_price, null, item.execution_price_change)}
                                </span>
                              </div>
                            )}
                            {isLoadingThis ? (
                              <div className={`overview-regional-row ${OVERVIEW_ROW_GRID} border-b border-slate-200 dark:border-slate-700 px-3.5 py-2 text-[12px] md:text-[13px] overview-exec-muted last:border-b-0`}>
                                <span className="truncate min-w-0">{text.loading}</span>
                                <span /><span />
                              </div>
                            ) : !regional ? (
                              <div className={`overview-regional-row ${OVERVIEW_ROW_GRID} border-b border-slate-200 dark:border-slate-700 px-3.5 py-2 text-[12px] md:text-[13px] overview-exec-muted last:border-b-0`}>
                                <span className="truncate min-w-0">{text.regionalUnavailable}</span>
                                <span className="text-right">--</span>
                                <span /><span />
                              </div>
                            ) : (
                              (() => {
                                const visibleRegions = regional.regions.filter(
                                  (r) => !HIDDEN_REGIONAL_PRICE_TYPES.has(String(r.price_type || '').trim().toLowerCase()),
                                );
                                if (visibleRegions.length === 0) {
                                  return (
                                    <div className={`overview-regional-row ${OVERVIEW_ROW_GRID} border-b border-slate-200 dark:border-slate-700 px-3.5 py-2 text-[12px] md:text-[13px] overview-exec-muted last:border-b-0`}>
                                      <span className="truncate min-w-0">{text.regionalUnavailable}</span>
                                      <span className="text-right">--</span>
                                      <span /><span />
                                    </div>
                                  );
                                }
                                return visibleRegions.map((r) => {
                                  const isStale = (r.days_stale ?? 0) > 7;
                                  return (
                                    <div key={r.price_type} className={`overview-regional-row ${OVERVIEW_ROW_GRID} border-b border-slate-200 dark:border-slate-700 px-3.5 py-1.5 text-[12px] md:text-[13px] last:border-b-0 ${r.is_internal ? 'overview-regional-internal' : ''}`}>
                                      <span className={`min-w-0 truncate ${r.is_internal ? 'font-semibold text-foreground' : 'overview-exec-muted'}`} title={
                                        language === 'en'
                                          ? r.is_internal ? 'Internal ERP reference price' : 'External market reference price'
                                          : r.is_internal ? '内部ERP参考价' : '外部市场参考价'
                                      }>
                                        {language === 'en'
                                          ? (REGIONAL_LABEL_EN[r.price_type] ?? r.label)
                                          : (REGIONAL_LABEL_ZH[r.price_type]
                                              ?? REGIONAL_LABEL_ZH[String(r.label || '').trim().toLowerCase()]
                                              ?? r.label)}
                                        {r.is_internal ? ' 🏭' : ''}
                                      </span>
                                      <span className={`overview-exec-muted text-right text-[11px] md:text-[12px] ${dateTextClass(r.date)}`} title={language === 'en' ? 'Date of this price quote' : '该报价的日期'}>
                                        {formatShortDate(r.date)}
                                      </span>
                                      <span className="overview-exec-value text-right font-mono text-[13px] md:text-[14px]" title={language === 'en' ? `Market price: ¥${r.price.toLocaleString(resolvedLocale)}/ton` : `市场价：¥${r.price.toLocaleString(resolvedLocale)}/吨`}>
                                        ¥{r.price.toLocaleString(resolvedLocale)}
                                      </span>
                                      <span className={`overview-exec-value text-right font-mono text-[12px] md:text-[13px] ${isStale ? 'overview-exec-muted' : changeColor(changeSignal(r.price, r.change_pct), 'text-slate-400')}`} title={
                                        language === 'en'
                                          ? r.change_pct != null
                                            ? `Percentage change vs previous market price: ${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(2)}%`
                                            : 'No change data available'
                                          : r.change_pct != null
                                            ? `较前次市场价涨跌幅：${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(2)}%`
                                            : '暂无涨跌幅数据'
                                      }>
                                        {formatAbsoluteChange(r.price, r.change_pct)}
                                      </span>
                                    </div>
                                  );
                                });
                              })()
                            )}
                          </div>

                          {/* Supply-chain KPI section (ERP products only) */}
                          {(() => {
                            const gradeKpi = gradeKpisByProduct[item.id];
                            const gradeRows = gradeKpi?.rows || [];
                            if (!ERP_PRODUCT_IDS.includes(item.id) || gradeRows.length === 0) return null;
                            return (
                              <div className="overview-grade-kpi border-t border-slate-200 dark:border-slate-700 bg-slate-50/60 dark:bg-slate-800/40">
                                <div className="px-3.5 pt-2 pb-1 text-[10px] md:text-[11px] font-semibold uppercase tracking-[0.05em] text-slate-400 dark:text-slate-500">
                                  {text.supplyChainKpiTitle}
                                </div>
                                <div className="grid grid-cols-[minmax(0,1fr)_64px_60px_48px_64px] gap-2 px-3.5 pb-1 text-[9px] md:text-[10px] font-semibold uppercase tracking-[0.03em] text-slate-400 dark:text-slate-500">
                                  <span>{text.grade}</span>
                                  <span className="text-right">{text.sales30d}</span>
                                  <span className="text-right">{text.inventory}</span>
                                  <span className="text-right">{text.inventoryDays}</span>
                                  <span className="text-right">{text.pending}</span>
                                </div>
                                <div className="px-3.5 pb-2">
                                  {gradeRows.map((r) => (
                                    <div key={r.sku_code || r.grade} className="grid grid-cols-[minmax(0,1fr)_64px_60px_48px_64px] items-center gap-2 border-t border-slate-100 dark:border-slate-800 py-1 text-[12px] md:text-[12px] first:border-t-0">
                                      <span className="min-w-0">
                                        <span className="block truncate font-medium text-foreground" title={r.grade}>{r.grade}</span>
                                        <span className="block truncate text-[10px] text-slate-400 dark:text-slate-500" title={r.sku_code}>{r.sku_code}</span>
                                      </span>
                                      <span className="text-right font-mono text-[11px] md:text-[12px] text-foreground">{formatKpiValue(r.sales_30d_tons)}</span>
                                      <span className="text-right font-mono text-[11px] md:text-[12px] text-foreground">{formatKpiValue(r.inventory_tons)}</span>
                                      <span className="text-right font-mono text-[11px] md:text-[12px] text-foreground">{formatKpiValue(r.inventory_days)}</span>
                                      <span className="text-right font-mono text-[11px] md:text-[12px] text-foreground">{formatKpiValue(r.pending_tons)}</span>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            );
                          })()}
                        </div>
                      );
                    })}
                  </div>
                </article>
              ))}
            </div>
          </section>

          {/* FORECAST section */}
          <section className="mb-5">
            <h3 className="mb-3 flex items-center gap-2 text-[12px] md:text-[13px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              <span className="block h-3 w-[3px] rounded bg-muted-foreground" />
              {text.futuresWeeklyGapTitle}
              {forecastLoading && (
                <span className="text-[10px] font-normal normal-case text-slate-400">{text.loading}</span>
              )}
            </h3>

            <div data-overview-grid="3col" className="grid grid-cols-1 gap-2.5 lg:grid-cols-2 xl:grid-cols-3">
              {groupedRows.map((group) => (
                <article key={`${group.key}-movement`} className="overflow-hidden rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900">
                  <div className={`${GROUP_VISUALS[group.key].futuresHeader} flex items-center justify-between px-3.5 py-2`}>
                    <h4 className="text-[12px] md:text-[13px] font-semibold">
                      {group.key === 'upstream' ? (language === 'zh' ? '上游' : 'Upstream')
                        : group.key === 'midstream' ? (language === 'zh' ? '中游' : 'Midstream')
                        : (language === 'zh' ? '下游' : 'Downstream')}
                    </h4>
                    <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] md:text-[11px] text-muted-foreground">
                      {group.rows.length} {text.items}
                    </span>
                  </div>

                  <div>
                    {group.rows.map((item) => {
                      const forecastBase = weeklyForecastBaseByProduct[item.id];
                      const hasForecast = typeof forecastBase === 'number' && Number.isFinite(forecastBase);
                      const snapshotGap = resolveGap(item.price, item.weekly_change_pct, item.weekly_gap_value);
                      const nextPrice = hasForecast
                        ? forecastBase
                        : (snapshotGap != null && Number.isFinite(snapshotGap))
                          ? item.price + snapshotGap
                          : null;
                      const forecastGap = hasForecast
                        ? Math.round((nextPrice - item.price) * 100) / 100
                        : snapshotGap;
                      return (
                        <article key={`${item.id}-move`} className="border-b border-slate-200 dark:border-slate-700 px-3.5 py-2.5 last:border-b-0">
                          <div className="grid grid-cols-[minmax(0,1fr)_minmax(110px,210px)_72px] items-center gap-2">
                            <div className="min-w-0">
                              <h5 className="truncate text-[13px] md:text-[14px] font-semibold text-slate-700 dark:text-slate-200" title={resolveName(item.id, item.name)}>
                                {resolveName(item.id, item.name)}
                              </h5>
                              <p className="mt-1 text-[11px] md:text-[12px] text-slate-400 dark:text-slate-500">{text.forecast7dLabel}</p>
                            </div>
                            <div className="text-left md:text-center text-[12px] md:text-[13px] text-slate-500 dark:text-slate-400">
                              {text.forecastLabel}:{' '}
                              <span className="font-semibold text-slate-600 dark:text-slate-200">{formatPriceValue(nextPrice)}</span>
                            </div>
                            <div className={`text-right font-mono text-[13px] md:text-[14px] font-semibold ${changeColor(forecastGap, 'text-slate-500')}`}>
                              {formatAbsoluteChange(item.price, null, forecastGap)}
                            </div>
                          </div>
                          {/* Phase 6: inline overlay badges */}
                          {(() => {
                            const ov = forecastOverlayByProduct[item.id];
                            if (!ov) return null;
                            const vol = ov.policy?.volatility_regime;
                            const intelCount = ov.intelligence?.active_event_count;
                            const intelDir = ov.intelligence?.bias_direction;
                            if (!vol && !intelCount) return null;
                            return (
                              <div className="flex flex-wrap gap-1 mt-1.5">
                                {vol && (
                                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[9px] font-medium leading-tight bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400">
                                    <span className="w-2 h-2 rounded-full bg-current inline-block" />
                                    {vol}
                                  </span>
                                )}
                                {intelCount > 0 && (
                                  <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[9px] font-medium leading-tight ${intelDir === 'up' ? 'bg-emerald-100 text-emerald-600 dark:bg-emerald-900/30 dark:text-emerald-400' : intelDir === 'down' ? 'bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400' : 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400'}`}>
                                    {intelDir === 'up' ? '↑' : intelDir === 'down' ? '↓' : '—'} {language === 'zh' ? '情报' : 'Intel'}
                                  </span>
                                )}
                              </div>
                            );
                          })()}
                        </article>
                      );
                    })}
                  </div>
                </article>
              ))}
            </div>
          </section>

          {/* INDUSTRY INTELLIGENCE section */}
          <section>
            <div className="mb-2 flex items-center justify-between gap-2">
              <h3 className="flex items-center gap-2 text-[11px] md:text-[12px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                <span className="block h-3 w-[3px] rounded bg-muted-foreground" />
                {text.industryIntelligenceTitle}
              </h3>
              <p className="text-[10px] md:text-[11px] text-slate-400 dark:text-slate-500">
                {language === 'en' ? 'Updated' : '更新'}: {formatNewsTimestamp(newsUpdatedAt, language, resolvedLocale)} · {news.length} {text.items}
              </p>
            </div>

            {newsLoading && news.length === 0 ? (
              <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3 text-[14px] md:text-[15px] text-slate-500">
                {text.loading}
              </div>
            ) : newsError ? (
              <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3 text-[14px] md:text-[15px] text-rose-600">
                {newsError}
              </div>
            ) : news.length === 0 ? (
              <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-3 text-[14px] md:text-[15px] text-slate-500">
                {text.noNews}
              </div>
            ) : (
              <div data-overview-grid="3col" className="grid grid-cols-1 gap-2.5 lg:grid-cols-3">
                {newsColumns.map((column, idx) => (
                  <article key={`news-col-${idx}`} className="overflow-hidden rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-[0_1px_3px_rgba(15,23,42,0.06)]">
                    <div className="flex items-center justify-between bg-card border-b border-border px-3.5 py-2.5 text-[12px] md:text-[13px] font-semibold uppercase tracking-[0.06em] text-foreground">
                      <span>{idx === 0 ? text.feedstockAndCrude : idx === 1 ? text.midstreamProducts : text.downstreamAndMarket}</span>
                      <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] md:text-[11px] text-muted-foreground">{column.length}</span>
                    </div>
                    <div>
                      {column.map((item) => (
                        <article key={item.event_id} className="border-b border-slate-200 dark:border-slate-700 px-3.5 py-3.5 last:border-b-0 hover:bg-slate-50/70 dark:hover:bg-slate-800/50 transition-colors">
                          <h4 className="line-clamp-2 text-[14px] md:text-[15px] font-semibold leading-snug text-slate-800 dark:text-slate-100">
                            {item.headline}
                          </h4>
                          {(() => {
                            const impact = weeklyImpactSignal(item);
                            const explanation = buildDirectNewsExplanation(item, language);
                            return (
                              <>
                                <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] md:text-[12px]">
                                  <span className={`rounded border px-1.5 py-0.5 font-semibold ${weeklyImpactTone(impact.direction)}`}>
                                    {weeklyDirectionLabel(impact.direction, language)}
                                  </span>
                                  <span className="text-muted-foreground">{item.source_label || 'Source'}</span>
                                  <span className="text-slate-400 dark:text-slate-500">{formatNewsTimestamp(item.detected_at, language, resolvedLocale)}</span>
                                </div>
                                <p className="mt-2 rounded-md border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/60 px-2.5 py-2 text-[12px] md:text-[13px] leading-relaxed text-slate-700 dark:text-slate-200">
                                  {explanation}
                                </p>
                              </>
                            );
                          })()}
                          {item.summary ? (
                            <p className="mt-1.5 line-clamp-2 text-[12px] md:text-[13px] leading-relaxed text-slate-500 dark:text-slate-400">
                              {item.summary}
                            </p>
                          ) : null}
                          {item.source_url ? (
                            <div className="mt-2">
                              <a href={item.source_url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center rounded border border-border bg-muted px-2 py-0.5 text-[11px] md:text-[12px] font-medium text-foreground hover:bg-secondary">
                                {text.openLink}
                              </a>
                            </div>
                          ) : null}
                        </article>
                      ))}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>

          <div className="mt-4 border-t border-slate-200 dark:border-slate-700 pt-2.5 text-[11px] md:text-[12px] text-slate-400 dark:text-slate-500">
            {text.sourcesNote}
          </div>
        </main>
      </div>
    </div>
  );
}
