import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  BookOpen, Loader2, Table2, Network, Sigma, TrendingUp, Filter, Search,
  Pencil, Check, X, Layers,
} from 'lucide-react';
import { appParams } from '@/lib/app-params';
import authFetch from '@/api/authFetch';

const APP_ID = appParams.appId || 'local-zhanlu-app';

/**
 * The SEMANTIC STORE of a database KnowledgeBase — the OCI "offline
 * enrichment → semantic store" layer made visible.
 *
 * Every item carries its provenance tier — VERIFIED (human-authored),
 * DISCOVERED (indexed), LEARNED_FROM_WORKLOAD (mined from query logs) —
 * the same tiers the online NL2SQL pipeline consumes as grounding.
 * Rendering nothing for non-database KBs or when the semantic store is
 * disabled.
 *
 * UX (2026-10-12): the old layout stacked 6 stat cards + 5 accordions —
 * ~150px of chrome, every section collapsed, two clicks to any content,
 * duplicate counts (cards AND headers), and the filter gave no feedback
 * until each section was opened. Now: one tab bar with live counts (the
 * tabs ARE the overview), the non-navigable stats (Described / Domains)
 * as a one-line micro-summary, content one click away, and per-tab match
 * counts while filtering so the user sees WHERE matches are.
 */

const TIER_STYLE = {
  VERIFIED: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  DISCOVERED: 'bg-sky-50 text-sky-700 border-sky-200',
  LEARNED_FROM_WORKLOAD: 'bg-amber-50 text-amber-700 border-amber-200',
  LEARNED: 'bg-amber-50 text-amber-700 border-amber-200',
  RUNTIME_INFERRED: 'bg-stone-50 text-stone-500 border-stone-200',
};

function TierBadge({ tier }) {
  if (!tier) return null;
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded border shrink-0 ${
        TIER_STYLE[tier] || 'border-stone-200 text-stone-500 bg-stone-50'
      }`}
    >
      {tier.replace('LEARNED_FROM_WORKLOAD', 'WORKLOAD').replace('_INFERRED', '')}
    </span>
  );
}

function Th({ children }) {
  return (
    <th className="text-left font-medium text-stone-500 px-3 py-2 border-b border-stone-200 whitespace-nowrap">
      {children}
    </th>
  );
}

function Td({ children, mono = false }) {
  return (
    <td
      className={`px-3 py-2 border-b border-stone-100 align-top ${
        mono ? 'font-mono text-[12px] text-stone-700' : 'text-[13px] text-stone-700'
      }`}
    >
      {children}
    </td>
  );
}

function EmptyHint({ children }) {
  return <p className="text-xs text-stone-400 px-1 py-3">{children}</p>;
}

export default function KbSemanticStore({ kbId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState('');
  const [tab, setTab] = useState('tables');
  const [editing, setEditing] = useState(null); // {table_meta_id, description_en, description_zh}
  const [saving, setSaving] = useState(false);

  const fetchStore = useCallback(async () => {
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/semantic-store`
      );
      if (!res.ok) {
        setFailed(true);
        return;
      }
      setData(await res.json());
    } catch {
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    fetchStore();
  }, [fetchStore]);

  const saveTableEdit = useCallback(async () => {
    if (!editing?.table_meta_id) return;
    setSaving(true);
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/catalog/tables/${editing.table_meta_id}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            description_en: editing.description_en || null,
            description_zh: editing.description_zh || null,
          }),
        }
      );
      if (res.ok) {
        // refresh the store so the new meaning (and VERIFIED-tier provenance
        // once the catalog re-reads it) shows immediately
        setEditing(null);
        setLoading(true);
        fetchStore();
      }
    } finally {
      setSaving(false);
    }
  }, [editing, kbId, fetchStore]);

  const q = query.trim().toLowerCase();

  const relations = useMemo(() => {
    const rels = data?.relations || [];
    if (!q) return rels;
    return rels.filter(
      (r) =>
        r.table_name?.toLowerCase().includes(q) ||
        (r.description || '').toLowerCase().includes(q) ||
        (r.aliases || []).some((a) => String(a).toLowerCase().includes(q)) ||
        (r.domain || '').toLowerCase().includes(q)
    );
  }, [data, q]);

  const glossary = useMemo(() => {
    const g = data?.glossary || [];
    if (!q) return g;
    return g.filter(
      (t) =>
        t.term?.toLowerCase().includes(q) ||
        (t.definition || '').toLowerCase().includes(q) ||
        (t.aliases || []).some((a) => String(a).toLowerCase().includes(q))
    );
  }, [data, q]);

  const joinsTotal = data?.joins_total || 0;
  const joins = useMemo(() => {
    const j = data?.joins || [];
    const filtered = q
      ? j.filter(
          (x) =>
            x.source_table?.toLowerCase().includes(q) ||
            x.target_table?.toLowerCase().includes(q)
        )
      : j;
    return filtered.slice(0, 100);
  }, [data, q]);

  const joinsFilteredTotal = useMemo(() => {
    const j = data?.joins || [];
    if (!q) return joinsTotal;
    return j.filter(
      (x) =>
        x.source_table?.toLowerCase().includes(q) ||
        x.target_table?.toLowerCase().includes(q)
    ).length;
  }, [data, q, joinsTotal]);

  const workload = data?.workload || [];
  const joinHints = data?.mined?.join_hints || [];
  const commonFilters = data?.mined?.common_filters || [];
  const hasPatterns = joinHints.length > 0 || commonFilters.length > 0;

  if (loading) {
    return (
      <div className="px-6 pt-5 flex items-center gap-2 text-sm text-stone-400">
        <Loader2 size={14} className="animate-spin" /> Loading semantic store…
      </div>
    );
  }
  if (failed || !data?.enabled) return null;

  const s = data.stats || {};

  // Tab bar = the overview. Each tab carries its (match-aware) count, so
  // the user sees where search hits are without clicking through sections.
  const tabs = [
    {
      id: 'tables',
      label: 'Tables',
      icon: Table2,
      count: relations.length,
      total: (data.relations || []).length,
    },
    {
      id: 'terms',
      label: 'Terms',
      icon: BookOpen,
      count: glossary.length,
      total: (data.glossary || []).length,
    },
    {
      id: 'joins',
      label: 'Join Paths',
      icon: Network,
      count: joins.length,
      total: q ? joinsFilteredTotal : joinsTotal,
    },
    ...(workload.length > 0 || s.workload_tables
      ? [{
          id: 'workload',
          label: 'Workload',
          icon: TrendingUp,
          count: workload.length,
          total: workload.length,
        }]
      : []),
    ...(hasPatterns
      ? [{
          id: 'patterns',
          label: 'Patterns',
          icon: Filter,
          count: joinHints.length + commonFilters.length,
          total: joinHints.length + commonFilters.length,
        }]
      : []),
  ];
  const active = tabs.find((t) => t.id === tab) || tabs[0];
  if (active.id !== tab) setTab(active.id);
  const filtering = !!q;
  const showMatchState = filtering && active.count !== active.total;

  return (
    <div className="px-6 pt-5 border-t border-stone-100 mt-5">
      <div className="flex items-center gap-2 flex-wrap">
        <BookOpen size={16} className="text-amber-600" />
        <span className="text-sm font-semibold text-stone-800">
          Semantic Store
        </span>
        <span className="text-xs text-stone-400">
          business meaning the NL2SQL pipeline is grounded in
        </span>
        <div className="ml-auto relative">
          <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-stone-400" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter tables / terms / joins…"
            className="pl-7 pr-7 py-1 text-xs border border-stone-200 rounded-md w-56 focus:outline-none focus:ring-1 focus:ring-amber-400"
          />
          {q && (
            <button
              onClick={() => setQuery('')}
              title="Clear filter"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 text-stone-300 hover:text-stone-500"
            >
              <X size={12} />
            </button>
          )}
        </div>
      </div>

      {/* Non-navigable micro-stats — the old card row, minus the duplication */}
      <div className="mt-1.5 flex items-center gap-3 text-[11px] text-stone-400">
        <span className="flex items-center gap-1">
          <Sigma size={11} className="text-amber-500/70" />
          {Number(s.columns_described ?? 0).toLocaleString()} columns described
        </span>
        <span className="flex items-center gap-1">
          <Layers size={11} className="text-amber-500/70" />
          {Number(s.domains ?? 0).toLocaleString()} domains
        </span>
      </div>

      {/* Tab bar with live counts — content is ONE click away */}
      <div className="mt-2.5 flex items-center gap-1 border-b border-stone-200 overflow-x-auto">
        {tabs.map(({ id, label, icon: Icon, count, total }) => {
          const selected = id === active.id;
          const hit = filtering && count > 0;
          return (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 -mb-px whitespace-nowrap transition-colors ${
                selected
                  ? 'border-amber-500 text-stone-800'
                  : 'border-transparent text-stone-500 hover:text-stone-700 hover:border-stone-300'
              }`}
            >
              <Icon size={13} className={selected ? 'text-amber-600' : 'text-stone-400'} />
              {label}
              <span
                className={`text-[10px] px-1.5 py-px rounded-full tabular-nums ${
                  hit
                    ? 'bg-amber-100 text-amber-800'
                    : selected
                      ? 'bg-stone-100 text-stone-500'
                      : 'bg-stone-50 text-stone-400'
                }`}
              >
                {Number(count).toLocaleString()}
                {total > count && !filtering ? `/${Number(total).toLocaleString()}` : ''}
              </span>
            </button>
          );
        })}
      </div>

      {showMatchState && (
        <p className="mt-2 text-[11px] text-stone-400">
          {active.count.toLocaleString()} match{active.count === 1 ? '' : 'es'} in{' '}
          {active.label.toLowerCase()}
          {filtering && <> for “<span className="text-stone-600">{q}</span>”</>}
          {' — '}other tabs show their match counts above.
        </p>
      )}

      {/* ── Tables & Business Meaning ── */}
      {active.id === 'tables' && (
        <div className="mt-3">
          {relations.length === 0 ? (
            <EmptyHint>
              {filtering
                ? 'No tables match the filter.'
                : 'No tables indexed yet.'}
            </EmptyHint>
          ) : (
            <div className="rounded-lg border border-stone-200 overflow-x-auto max-h-96 overflow-y-auto">
              <table className="w-full text-left">
                <thead className="sticky top-0 bg-white">
                  <tr>
                    <Th>Table</Th>
                    <Th>Role</Th>
                    <Th>Domain</Th>
                    <Th>Rows</Th>
                    <Th>Description / Aliases</Th>
                    <Th>Tier</Th>
                  </tr>
                </thead>
                <tbody>
                  {relations.map((r) => (
                    <tr key={`${r.kb_id}:${r.table_name}`} className="hover:bg-stone-50/60">
                      <Td mono>{r.table_name}</Td>
                      <Td>{r.role !== 'unknown' ? r.role : '—'}</Td>
                      <Td>{r.domain || '—'}</Td>
                      <Td>
                        {r.row_count != null
                          ? Number(r.row_count).toLocaleString()
                          : '—'}
                      </Td>
                      <Td>
                        {editing?.table_meta_id === r.table_meta_id ? (
                          <div className="space-y-1 min-w-[240px]">
                            <input
                              value={editing.description_en}
                              onChange={(e) =>
                                setEditing({ ...editing, description_en: e.target.value })
                              }
                              placeholder="English description"
                              className="w-full text-[12px] border border-stone-300 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-amber-400"
                            />
                            <input
                              value={editing.description_zh || ''}
                              onChange={(e) =>
                                setEditing({ ...editing, description_zh: e.target.value })
                              }
                              placeholder="中文描述"
                              className="w-full text-[12px] border border-stone-300 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-amber-400"
                            />
                            <div className="flex gap-1">
                              <button
                                onClick={saveTableEdit}
                                disabled={saving}
                                className="flex items-center gap-1 text-[11px] px-2 py-0.5 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
                              >
                                <Check size={11} /> {saving ? 'Saving…' : 'Save'}
                              </button>
                              <button
                                onClick={() => setEditing(null)}
                                className="flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border border-stone-300 text-stone-600 hover:bg-stone-100"
                              >
                                <X size={11} /> Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="flex items-start gap-1.5">
                            <span>
                              {r.description || '—'}
                              {(r.aliases || []).length > 0 && (
                                <span className="block text-[11px] text-stone-400">
                                  aka: {r.aliases.join(', ')}
                                </span>
                              )}
                            </span>
                            {r.table_meta_id && (
                              <button
                                onClick={() =>
                                  setEditing({
                                    table_meta_id: r.table_meta_id,
                                    description_en: r.description || '',
                                    description_zh: r.description_alt || '',
                                  })
                                }
                                title="Edit meaning — a human edit is the strongest provenance"
                                className="text-stone-300 hover:text-amber-600 shrink-0"
                              >
                                <Pencil size={11} />
                              </button>
                            )}
                          </div>
                        )}
                      </Td>
                      <Td>
                        <TierBadge tier={r.tier} />
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Business terms & metric definitions ── */}
      {active.id === 'terms' && (
        <div className="mt-3">
          {glossary.length === 0 ? (
            <EmptyHint>
              {filtering
                ? 'No terms match the filter.'
                : 'No terms or metric definitions authored yet. Verified vocabulary (terminologies, metric definitions, aliases) that schema linking uses to resolve business words would appear here.'}
            </EmptyHint>
          ) : (
            <div className="rounded-lg border border-stone-200 overflow-x-auto max-h-96 overflow-y-auto">
              <table className="w-full text-left">
                <thead className="sticky top-0 bg-white">
                  <tr>
                    <Th>Term</Th>
                    <Th>Definition</Th>
                    <Th>SQL expression</Th>
                    <Th>Unit</Th>
                    <Th>Agg</Th>
                    <Th>Tier</Th>
                  </tr>
                </thead>
                <tbody>
                  {glossary.map((t) => (
                    <tr key={`${t.origin}:${t.term}`} className="hover:bg-stone-50/60">
                      <Td mono>
                        {t.term}
                        {(t.aliases || []).length > 0 && (
                          <span className="block text-[11px] text-stone-400">
                            {t.aliases.join(', ')}
                          </span>
                        )}
                      </Td>
                      <Td>{t.definition}</Td>
                      <Td mono>{t.sql_expression || '—'}</Td>
                      <Td>{t.unit || '—'}</Td>
                      <Td>{t.aggregation || '—'}</Td>
                      <Td>
                        <TierBadge tier={t.tier} />
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Join paths ── */}
      {active.id === 'joins' && (
        <div className="mt-3">
          {joins.length === 0 ? (
            <EmptyHint>
              {filtering ? 'No join paths match the filter.' : 'No join paths discovered yet.'}
            </EmptyHint>
          ) : (
            <>
              {!filtering && joinsTotal > joins.length && (
                <p className="mb-1.5 text-[11px] text-stone-400">
                  Showing the first {joins.length} of {joinsTotal.toLocaleString()} — use the
                  filter to narrow to a table.
                </p>
              )}
              <div className="rounded-lg border border-stone-200 overflow-x-auto max-h-80 overflow-y-auto">
                <table className="w-full text-left">
                  <thead className="sticky top-0 bg-white">
                    <tr>
                      <Th>Source</Th>
                      <Th>→</Th>
                      <Th>Target</Th>
                      <Th>Columns</Th>
                      <Th>Type</Th>
                      <Th>Confidence</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {joins.map((j, i) => (
                      <tr key={i} className="hover:bg-stone-50/60">
                        <Td mono>{j.source_table}</Td>
                        <Td className="text-stone-300">→</Td>
                        <Td mono>{j.target_table}</Td>
                        <Td mono>
                          {(j.source_columns || []).join(', ')} ={' '}
                          {(j.target_columns || []).join(', ')}
                        </Td>
                        <Td>{j.relation_type}</Td>
                        <Td>
                          {j.confidence != null
                            ? `${Math.round(j.confidence * 100)}%`
                            : '—'}
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {/* ── Workload signals ── */}
      {active.id === 'workload' && (
        <div className="mt-3">
          {workload.length === 0 ? (
            <EmptyHint>
              {filtering
                ? 'No workload signals match the filter.'
                : 'No query workload recorded yet — signals appear as the NL2SQL pipeline runs.'}
            </EmptyHint>
          ) : (
            <div className="rounded-lg border border-stone-200 overflow-x-auto max-h-72 overflow-y-auto">
              <table className="w-full text-left">
                <thead className="sticky top-0 bg-white">
                  <tr>
                    <Th>Table</Th>
                    <Th>Uses</Th>
                    <Th>Successes</Th>
                    <Th>Failures</Th>
                    <Th>Last used</Th>
                    <Th>Tier</Th>
                  </tr>
                </thead>
                <tbody>
                  {workload.map((w) => (
                    <tr key={w.table_name} className="hover:bg-stone-50/60">
                      <Td mono>{w.table_name}</Td>
                      <Td>{w.uses}</Td>
                      <Td>{w.successes}</Td>
                      <Td>{w.failures}</Td>
                      <Td>{w.last_used ? String(w.last_used).slice(0, 19) : '—'}</Td>
                      <Td>
                        <TierBadge tier={w.tier} />
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Mined patterns ── */}
      {active.id === 'patterns' && (
        <div className="mt-3 rounded-lg border border-stone-200 divide-y divide-stone-100 max-h-72 overflow-y-auto">
          {joinHints.length === 0 && commonFilters.length === 0 ? (
            <EmptyHint>
              {filtering
                ? 'No patterns match the filter.'
                : 'No patterns mined yet — recurring joins and filters from successful queries appear here.'}
            </EmptyHint>
          ) : (
            <>
              {joinHints.map((h, i) => (
                <div key={`jh-${i}`} className="px-4 py-2 text-xs">
                  <span className="font-mono text-stone-700">
                    {h.table_a} ⋈ {h.table_b}
                  </span>
                  <span className="ml-2 text-stone-400">
                    joined {h.occurrences}× in successful queries
                  </span>
                </div>
              ))}
              {commonFilters.map((f, i) => (
                <div key={`cf-${i}`} className="px-4 py-2 text-xs">
                  <span className="font-mono text-stone-700">
                    {f.table}.{f.filter}
                  </span>
                  <span className="ml-2 text-stone-400">
                    recurring filter ({f.occurrences}×)
                  </span>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
