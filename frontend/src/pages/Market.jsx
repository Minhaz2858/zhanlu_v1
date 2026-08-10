import { useState, useEffect, useMemo } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import PageHeader from '@/components/PageHeader';
import { Star, Users, Loader2, Search, PackageOpen, X, Tag } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

// Map existing `category` values to the three new filter dimensions.
// industry → 行业 / role → 角色(office) / function → 职能(ops_governance, data_processing)
const CATEGORY_TO_KIND = {
  industry: 'industry',
  office: 'role',
  ops_governance: 'function',
  data_processing: 'function',
};

function getAgentKinds(a) {
  const kinds = new Set();
  const mapped = CATEGORY_TO_KIND[a.category];
  if (mapped) kinds.add(mapped);
  return [...kinds];
}

const FILTER_TAGS = ['industry', 'role', 'function'];

export default function Market() {
  const { t, lang } = useLanguage();
  const [agents, setAgents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedKinds, setSelectedKinds] = useState(new Set());
  const [query, setQuery] = useState('');
  const navigate = useNavigate();

  useEffect(() => { load(); }, []);
  async function load() {
    try {
      setAgents(await base44.entities.MarketAgent.list('-updated_date', 200));
    } finally { setLoading(false); }
  }

  const translate = useTranslate(
    agents.flatMap((a) => [a.name, a.description, ...((a.capabilities || []).filter(Boolean))]),
    lang
  );

  function toggleKind(k) {
    setSelectedKinds((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }

  const filtered = useMemo(() => {
    let result = agents;
    // kind filter: AND across dimensions, OR within each dimension (single-dimension here)
    if (selectedKinds.size > 0) {
      result = result.filter((a) => {
        const kinds = getAgentKinds(a);
        return kinds.some((k) => selectedKinds.has(k));
      });
    }
    // search: match name, description, capabilities
    if (query.trim()) {
      const q = query.trim().toLowerCase();
      result = result.filter((a) => {
        const haystack = [
          a.name || '',
          a.description || '',
          ...(a.capabilities || []),
        ].join(' ').toLowerCase();
        return haystack.includes(q);
      });
    }
    return result;
  }, [agents, selectedKinds, query]);

  const countText = t.market.count.replace('{count}', filtered.length);

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader title={t.market.title} subtitle={t.market.subtitle} />

      {/* Filter bar: tags + search in one row */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        {/* Three multi-select filter pill tags */}
        <div className="flex flex-wrap items-center gap-1.5">
          {FILTER_TAGS.map((k) => {
            const active = selectedKinds.has(k);
            return (
              <button
                key={k}
                onClick={() => toggleKind(k)}
                className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-xs font-medium transition-all ${
                  active
                    ? 'border-primary/40 bg-primary/10 text-primary'
                    : 'border-border bg-card text-muted-foreground hover:border-primary/30 hover:text-foreground'
                }`}
              >
                {t.market.filterTags[k]}
                {active && <X className="h-3 w-3" />}
              </button>
            );
          })}
          {selectedKinds.size > 0 && (
            <button
              onClick={() => setSelectedKinds(new Set())}
              className="inline-flex items-center gap-1 rounded-full border border-border bg-transparent px-2 py-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
            >
              <X className="h-3 w-3" /> {lang === 'en' ? 'Clear' : '清除'}
            </button>
          )}
        </div>

        {/* Search input on the right side */}
        <div className="relative flex-1 min-w-[180px]">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t.market.searchPlaceholder}
            className="w-full rounded-lg border border-border bg-card py-1.5 pl-8 pr-3 text-xs text-foreground placeholder:text-muted-foreground focus:border-primary/50 focus:outline-none focus:ring-1 focus:ring-primary/20"
          />
        </div>
      </div>

      {/* Count line */}
      <p className="mb-4 text-xs text-muted-foreground">{countText}</p>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border py-20 text-center">
          <PackageOpen className="h-8 w-8 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">{t.market.emptyFiltered}</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filtered.map((a) => {
            const kinds = getAgentKinds(a);
            return (
              <button key={a.id} onClick={() => navigate(`/market/${a.id}`)} className="group flex flex-col rounded-xl border border-border bg-card p-5 text-left transition-shadow hover:shadow-md hover:border-primary/20">
                <h3 className="mb-2 min-w-0 break-words font-display text-base text-foreground group-hover:text-primary">{translate(a.name)}</h3>
                <p className="mb-3 min-w-0 flex-1 break-words text-xs text-muted-foreground line-clamp-2">{translate(a.description)}</p>
                {/* Kind chips */}
                <div className="mb-3 flex flex-wrap gap-1.5">
                  {kinds.map((k) => (
                    <span key={k} className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">
                      <Tag className="h-2.5 w-2.5" /> {t.market.filterTags[k]}
                    </span>
                  ))}
                </div>
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span className="inline-flex items-center gap-1"><Star className="h-3 w-3 fill-primary text-primary" /> {a.rating}</span>
                  <span className="inline-flex items-center gap-1"><Users className="h-3 w-3" /> {a.subscribers}</span>
                </div>
              </button>
            );
          })}
        </div>
      )}

    </div>
  );
}
