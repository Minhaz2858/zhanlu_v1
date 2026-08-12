import { useState, useEffect, useCallback } from 'react';
import { RefreshCw, Globe, Trash2, EyeOff, Search, AlertCircle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { formatDate } from '@/lib/time';
import { listSourceSkills, syncSource } from '@/api/marketplace';
import SkillCard from './SkillCard';

/**
 * MarketplaceTab — renders the skill grid for a SINGLE source.
 *
 * The source list (and the "Add Source" / "Hide" / "Remove" controls) live
 * in Toolkit.jsx now, not here. This component is just the grid for the
 * currently-selected source plus its search/sync controls.
 */
export default function MarketplaceTab({
  source,
  installedNames,
  onInstall,
  onOpenDetail,
  onDelete,
  busy = false,
}) {
  const { t } = useLanguage();
  const [skills, setSkills] = useState([]);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');

  const loadSkills = useCallback(async (q = '') => {
    setLoading(true);
    setError('');
    try {
      const data = await listSourceSkills(source.id, { q, sort: 'name', limit: 100 });
      setSkills(data.skills || []);
      setCount(data.count || 0);
    } catch (e) {
      setSkills([]);
      setError(e.message || 'Failed to load skills');
    } finally {
      setLoading(false);
    }
  }, [source.id]);

  useEffect(() => {
    setSearch('');
    loadSkills('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source.id]);

  const handleSync = async () => {
    setSyncing(true);
    try {
      await syncSource(source.id);
      await loadSkills(search);
    } catch (e) {
      setError(e.message || 'Sync failed');
    } finally {
      setSyncing(false);
    }
  };

  const handleSearch = (e) => {
    const v = e.target.value;
    setSearch(v);
    loadSkills(v);
  };

  if (!source) return null;

  return (
    <div>
      <div className="flex items-center justify-between border-b border-border pb-3">
        <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
          <Globe className="h-3.5 w-3.5 shrink-0 text-primary" />
          <span className="truncate">{source.url}</span>
          <span className="text-muted-foreground/50">·</span>
          <span className="shrink-0">{source.skill_count} skills</span>
          <span className="text-muted-foreground/50">·</span>
          <span className="shrink-0">
            {source.last_sync_status === 'success'
              ? `synced ${formatDate(source.last_synced_at || Date.now())}`
              : source.last_sync_status}
          </span>
        </div>
        <div className="flex shrink-0 gap-1">
          <button
            onClick={handleSync}
            disabled={syncing}
            className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3 w-3 ${syncing ? 'animate-spin' : ''}`} />
            {t.marketplace?.sync || 'Sync'}
          </button>
          {source.is_default ? (
            <>
              {/* Hide = soft delete (reversible). Delete = hard delete
                  with CASCADE on external_skills. The Toolkit handler
                  shows an in-app confirm dialog before doing either.
                  ``busy`` is set by the parent while the delete API
                  call is in flight, so the buttons disable to
                  prevent a double-click from firing two DELETEs. */}
              <button
                onClick={() => !busy && onDelete?.(source, { action: 'hide' })}
                disabled={busy}
                title="Hide this source from the marketplace tab. You can re-show it later."
                className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
              >
                <EyeOff className="h-3 w-3" /> {t.marketplace?.hide || 'Hide'}
              </button>
              <button
                onClick={() => !busy && onDelete?.(source, { action: 'delete' })}
                disabled={busy}
                title="Permanently remove this source and all its skills. The source may reappear on the next backend restart if it is curated."
                className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-50"
              >
                <Trash2 className="h-3 w-3" /> {t.marketplace?.delete || 'Delete'}
              </button>
            </>
          ) : (
            <button
              onClick={() => !busy && onDelete?.(source, { action: 'delete' })}
              disabled={busy}
              title="Remove this source and all its skills."
              className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-50"
            >
              <Trash2 className="h-3 w-3" /> {t.marketplace?.delete || 'Delete'}
            </button>
          )}
        </div>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
          <input
            value={search}
            onChange={handleSearch}
            placeholder={t.marketplace?.search || 'Search skills...'}
            className="w-full rounded-lg border border-border bg-card py-1.5 pl-8 pr-3 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/30"
          />
        </div>
      </div>

      {error && (
        <div className="mt-3 flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2">
          <AlertCircle className="h-4 w-4 text-red-400" />
          <span className="text-xs text-red-300">{error}</span>
        </div>
      )}

      {loading ? (
        <div className="py-8 text-center text-sm text-muted-foreground">Loading skills...</div>
      ) : skills.length === 0 ? (
        <div className="py-8 text-center text-sm text-muted-foreground">No skills found in this source.</div>
      ) : (
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {skills.map((sk) => (
            <SkillCard
              key={sk.id}
              skill={sk}
              isInstalled={installedNames.has(sk.name)}
              onInstall={onInstall}
              onOpenDetail={onOpenDetail}
            />
          ))}
        </div>
      )}
    </div>
  );
}
