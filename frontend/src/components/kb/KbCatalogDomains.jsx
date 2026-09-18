import { useState, useEffect, useCallback } from 'react';
import { Layers, ChevronRight, ChevronDown, Loader2 } from 'lucide-react';
import { appParams } from '@/lib/app-params';
import authFetch from '@/api/authFetch';

const APP_ID = appParams.appId || 'local-zhanlu-app';

/**
 * Discovered business domains for a database KnowledgeBase.
 *
 * Renders the semantic layer's grouping output (name en/zh, description,
 * member count, how it was named) with each domain's tables and their
 * per-column semantic typing. Renders nothing when the KB has no domain map —
 * domains are produced by the catalog indexer, so an unindexed or non-database
 * KB simply has none.
 */
export default function KbCatalogDomains({ kbId }) {
  const [domains, setDomains] = useState([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(null);

  const fetchDomains = useCallback(async () => {
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/catalog/domains`
      );
      if (!res.ok) {
        setDomains([]);
        setLoading(false);
        return;
      }
      const data = await res.json();
      setDomains(data.domains || []);
    } catch {
      setDomains([]);
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    fetchDomains();
  }, [fetchDomains]);

  if (loading || domains.length === 0) return null;

  return (
    <div className="px-6 pt-5">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 text-sm font-semibold text-stone-700 hover:text-amber-700 transition-colors"
      >
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <Layers size={15} className="text-amber-600" />
        业务域 <span className="text-stone-400 font-normal">({domains.length})</span>
      </button>

      {open && (
        <div className="mt-3 rounded-lg border border-stone-200 divide-y divide-stone-100 overflow-hidden">
          {domains.map((d) => {
            const isOpen = expanded === d.id;
            return (
              <div key={d.id}>
                <button
                  onClick={() => setExpanded(isOpen ? null : d.id)}
                  className="w-full flex items-center gap-3 px-4 py-2.5 text-left hover:bg-stone-50/60 transition-colors"
                >
                  <span className="text-xs tabular-nums text-stone-400 w-8 text-right shrink-0">
                    {d.member_count}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="text-[13px] font-medium text-stone-800">
                      {d.name_en}
                    </span>
                    {d.name_zh && (
                      <span className="ml-2 text-xs text-stone-500">{d.name_zh}</span>
                    )}
                    {d.description_en && (
                      <span className="block text-xs text-stone-500 truncate">
                        {d.description_en}
                      </span>
                    )}
                  </span>
                  {d.method !== 'llm' && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded border border-stone-200 text-stone-500 shrink-0">
                      derived
                    </span>
                  )}
                </button>

                {isOpen && (
                  <div className="bg-stone-50/50 px-4 py-3 space-y-2">
                    {(d.tables || []).map((t) => (
                      <div key={t.table_meta_id || t.table_name} className="text-xs">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-[12px] text-stone-700">
                            {t.table_name}
                          </span>
                          {t.table_role && t.table_role !== 'unknown' && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded border border-stone-200 text-stone-500">
                              {t.table_role}
                            </span>
                          )}
                        </div>
                        {(t.measures || []).length > 0 && (
                          <div className="mt-1 text-stone-500">
                            度量:{' '}
                            {t.measures
                              .slice(0, 6)
                              .map((m) =>
                                m.unit ? `${m.column}(${m.unit})` : m.column
                              )
                              .join(', ')}
                          </div>
                        )}
                        {Object.keys(t.value_dictionary || {}).length > 0 && (
                          <div className="mt-0.5 text-stone-500">
                            取值域:{' '}
                            {Object.entries(t.value_dictionary)
                              .slice(0, 3)
                              .map(
                                ([col, vals]) =>
                                  `${col} = ${(vals || []).slice(0, 6).join('/')}`
                              )
                              .join('; ')}
                          </div>
                        )}
                        {(t.date_columns || []).length > 0 && (
                          <div className="mt-0.5 text-stone-400">
                            时间列: {t.date_columns.slice(0, 4).join(', ')}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
      {loading && <Loader2 size={14} className="animate-spin text-stone-400" />}
    </div>
  );
}
