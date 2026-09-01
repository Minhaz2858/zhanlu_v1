import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { formatAbsoluteTime } from '@/lib/time';
import { getAnonymousId } from '@/lib/anonymousId';
import SkillCardGrid from '@/components/toolkit/SkillCardGrid';
import MySkillsModal from '@/components/toolkit/MySkillsModal';
import SkillUploadDialog from '@/components/toolkit/SkillUploadDialog';
import McpSection from '@/components/toolkit/McpSection';
import SourceCard from '@/components/marketplace/SourceCard';
import MarketplaceTab from '@/components/marketplace/MarketplaceTab';
import AddMarketplaceDialog from '@/components/marketplace/AddMarketplaceDialog';
import SkillDetailSheet from '@/components/marketplace/SkillDetailSheet';
import {
  listSources, installSkill, deleteSource, listMySkills,
  listRemovedSources, restoreRemovedSource,
} from '@/api/marketplace';
import { Plug, Loader2, Plus, Sparkles, UploadCloud, User, Search, ChevronDown, Globe, AlertCircle, Undo2, Eye, EyeOff } from 'lucide-react';
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu';
import { toast } from '@/components/ui/use-toast';
import ConfirmDialog from '@/components/ui/ConfirmDialog';

export default function Toolkit() {
  const { t } = useLanguage();
  const [tools, setTools] = useState([]);
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [mainTab, setMainTab] = useState('skills');
  const [mySkillsOpen, setMySkillsOpen] = useState(false);
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('popular');
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadMode, setUploadMode] = useState('file');
  // Names of skills currently being added; used to disable the `+` button
  // mid-flight so a fast double-click doesn't create duplicate rows.
  const [adding, setAdding] = useState(() => new Set());
  const navigate = useNavigate();
  // Used to keep the `+` button disabled (or hide it) across re-renders
  // even when the network response hasn't arrived yet. Stored as a
  // ref so optimistic state updates don't cause a re-render.
  const optimisticRef = useRef(new Set());

  // ─── Browse Marketplace state ──────────────────────────────────────────
  // The marketplace tab is no longer a separate route — it lives inside
  // this page as a real tab. We carry the source list, the currently
  // active source, and the user's installed-from-marketplace skill names
  // (for the "Added" checkmark on each card) locally.
  const [marketSources, setMarketSources] = useState([]);
  const [marketSourcesLoading, setMarketSourcesLoading] = useState(false);
  const [marketSourcesError, setMarketSourcesError] = useState('');
  const [activeSourceId, setActiveSourceId] = useState(null);
  const [addSourceOpen, setAddSourceOpen] = useState(false);
  const [installedMarketNames, setInstalledMarketNames] = useState(() => new Set());
  const [detailSkill, setDetailSkill] = useState(null);
  // Removed curated sources panel. ``removedSources`` is the list of
  // URLs the user has explicitly hard-deleted. ``removedPanelOpen``
  // toggles a small "Show removed (N)" UI in the marketplace tab so
  // the user can see what's gone and click "Restore" if they change
  // their mind. ``restoringUrl`` is the URL currently being restored
  // (drives the spinner on the restore button).
  const [removedSources, setRemovedSources] = useState([]);
  const [removedPanelOpen, setRemovedPanelOpen] = useState(false);
  const [restoringUrl, setRestoringUrl] = useState(null);
  // First source load after switching INTO the marketplace tab. Keeps
  // the tab switch snappy — the source row appears instantly and the
  // first card's skills fetch happens in the background.
  const hasLoadedMarketOnce = useRef(false);

  useEffect(() => { load(); loadUser(); }, []);

  async function loadUser() {
    try { setUser(await base44.auth.me()); } catch { /* noop */ }
  }

  async function load() {
    try { setTools(await base44.entities.Tool.list('-updated_date', 200)); }
    finally { setLoading(false); }
  }

  function openBuildWithAgent() { navigate('/skill-agent?action=create'); }
  function openUploadSkill() { setUploadMode('file'); setUploadOpen(true); }
  function openGithubCollect() { setUploadMode('github'); setUploadOpen(true); }
  function openEditSkill(item) { navigate(`/skill-agent?skill=${item.id}`); }

  // The "owner" we filter "My Skills" by. If a real user is logged in
  // we use their id; otherwise we fall back to the SDK's per-browser
  // analytics session id, which is the same value the SDK attaches to
  // the `X-Base44-Anonymous-Id` header on every unauthenticated axios
  // call — so the backend stamps any row this browser creates with the
  // same value, and the filter here can find it.
  const ownerId = user?.id || getAnonymousId();

  async function addToMySkills(item) {
    const key = (item.name || '').toLowerCase();
    if (!key) return;
    if (optimisticRef.current.has(key) || adding.has(key)) return; // dedupe
    // Already in My Skills? No-op. The marketplace page may show the same
    // skill name twice (once as ``source='marketplace'`` and once as the
    // legacy ``source='builtin'`` duplicate from before the de-duplication
    // work), so the user-visible "Added" checkmark can already be true
    // for a sibling row whose ``created_by_id`` is already us.
    if (item.created_by_id && String(item.created_by_id) === String(ownerId)) {
      optimisticRef.current.add(key);
      return;
    }
    optimisticRef.current.add(key);
    setAdding((prev) => {
      const next = new Set(prev);
      next.add(key);
      return next;
    });
    try {
      // CLAIM the existing row by stamping it with our owner id — do
      // NOT create a new row. The previous behavior was to call
      // ``Tool.create(...)`` and copy every field, which produced a
      // duplicate row for every skill the user added (one
      // ``source='marketplace'`` row plus a new ``source='builtin'`` row
      // with the same name), and the marketplace page then displayed
      // both as separate cards. The My Skills filter
      // (``created_by_id === ownerId``) finds the claimed row, the
      // marketplace list shows the original row with a checkmark, and
      // no duplicate is ever created.
      await base44.entities.Tool.update(item.id, { created_by_id: ownerId });
      await load();
    } catch (err) {
      // Roll back the optimistic state so the user can retry.
      optimisticRef.current.delete(key);
      setAdding((prev) => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
      console.error('addToMySkills failed', err);
    }
  }
  async function removeFromMySkills(item) {
    // Un-claim a previously-claimed row. For a marketplace skill this
    // means clearing ``created_by_id`` so the user can re-claim it
    // later if they change their mind. For a truly user-owned skill
    // (source 'custom'/'user') we hard-delete it instead because the
    // user originally authored the content and removing ownership
    // without removing the row would leave a stranded record.
    const isClaimedMarketplace = item.source === 'marketplace';
    if (isClaimedMarketplace) {
      await base44.entities.Tool.update(item.id, { created_by_id: null });
    } else {
      await base44.entities.Tool.delete(item.id);
    }
    load();
  }
  // Kept for back-compat with the existing MySkillsModal which calls
  // ``onDelete`` for the trash action. We dispatch to removeFromMySkills
  // (un-claim for marketplace, hard-delete for user-authored) instead
  // of always hard-deleting — otherwise a claimed marketplace row would
  // be wiped from the catalog entirely, which is not what "remove from
  // My Skills" should do.
  async function deleteSkill(item) {
    if (!window.confirm(t.toolkit.delete + '?')) return;
    await removeFromMySkills(item);
  }

  // "My Skills" = rows the current owner created. Use `ownerId` (real
  // user id when available, otherwise the per-browser anonymous id) so
  // unauthenticated users still see what they `+`'d.
  const mySkills = ownerId ? tools.filter((x) => x.created_by_id === ownerId) : [];
  const mySkillNames = new Set([
    ...mySkills.map((x) => (x.name || '').toLowerCase()),
    ...optimisticRef.current,
  ]);
  let filtered = tools;
  if (filter === 'official') {
    filtered = filtered.filter((x) => (x.publisher || '').toLowerCase() === 'minimax' || x.source === 'builtin');
  } else if (filter === 'community') {
    filtered = filtered.filter((x) => !((x.publisher || '').toLowerCase() === 'minimax' || x.source === 'builtin'));
  }
  if (query) {
    const q = query.toLowerCase();
    filtered = filtered.filter((x) => (x.name || '').toLowerCase().includes(q) || (x.description || '').toLowerCase().includes(q));
  }
  if (sort === 'popular') filtered = [...filtered].sort((a, b) => (b.call_count || 0) - (a.call_count || 0));
  else if (sort === 'name') filtered = [...filtered].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  else if (sort === 'newest') filtered = [...filtered].sort((a, b) => new Date(b.created_date || 0) - new Date(a.created_date || 0));

  // ─── Browse Marketplace handlers ──────────────────────────────────────
  const loadMarketplace = useCallback(async () => {
    setMarketSourcesLoading(true);
    setMarketSourcesError('');
    try {
      const [sourcesData, mySkillsData, removedData] = await Promise.all([
        listSources(),
        listMySkills().catch(() => ({ skills: [] })),
        listRemovedSources().catch(() => ({ removed: [] })),
      ]);
      const srcs = sourcesData.sources || [];
      setMarketSources(srcs);
      setInstalledMarketNames(new Set((mySkillsData.skills || []).map((s) => s.name)));
      setRemovedSources(removedData.removed || []);
      // Default the active card to the first curated source if the user
      // hasn't picked one yet this session.
      setActiveSourceId((prev) => prev || srcs[0]?.id || null);
    } catch (e) {
      setMarketSourcesError(e.message || 'Failed to load marketplace');
    } finally {
      setMarketSourcesLoading(false);
    }
  }, []);

  // Restore a previously-deleted curated source. The endpoint clears
  // the tombstone and re-seeds the source from the seed definition.
  // We then add the restored source to local state immediately so the
  // user sees it appear in the source row without a full refresh.
  const handleRestoreRemoved = async (url) => {
    setRestoringUrl(url);
    try {
      const restored = await restoreRemovedSource(url);
      setRemovedSources((prev) => prev.filter((r) => r.url !== url));
      setMarketSources((prev) => {
        if (prev.some((s) => s.id === restored.id)) return prev;
        return [...prev, restored];
      });
      setActiveSourceId((prev) => prev || restored.id);
      toast({
        title: `Restored "${restored.name}"`,
        description: 'The source is back in the marketplace tab. Click Sync to fetch its skills.',
      });
    } catch (e) {
      console.error('[marketplace] restore failed:', e);
      toast({
        title: 'Failed to restore source',
        description: e.message || 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setRestoringUrl(null);
    }
  };

  // Load the marketplace lazily — only when the user opens the tab.
  // Keeps the initial Toolkit render fast and avoids hitting the API
  // for users who never use the marketplace.
  useEffect(() => {
    if (mainTab === 'marketplace' && !hasLoadedMarketOnce.current) {
      hasLoadedMarketOnce.current = true;
      loadMarketplace();
    }
  }, [mainTab, loadMarketplace]);

  // Poll the source list while any source is syncing. Curated sources
  // kick off an async sync the first time the marketplace tab is
  // opened, so the user opens the tab → sees "Syncing..." → the count
  // updates within a few seconds. The poll stops as soon as no source
  // reports ``last_sync_status === "syncing"``. We never poll when the
  // tab is hidden to avoid wasted bandwidth.
  useEffect(() => {
    if (mainTab !== 'marketplace') return;
    const anySyncing = marketSources.some((s) => s.last_sync_status === 'syncing');
    if (!anySyncing) return;
    const handle = setTimeout(() => { loadMarketplace(); }, 2000);
    return () => clearTimeout(handle);
  }, [mainTab, marketSources, loadMarketplace]);

  const handleMarketInstall = async (skill) => {
    try {
      await installSkill(skill.id);
      setInstalledMarketNames((prev) => new Set([...prev, skill.name]));
    } catch (e) {
      console.error('Install failed:', e);
    }
  };

  // ``source.is_default`` sources are handled two ways:
  //   * Hide   — soft delete, is_hidden=True. Reversible. The card
  //     disappears from the UI but the source + skills stay in the DB
  //     so the user can re-show it later.
  //   * Delete — hard delete. Fires the CASCADE on external_skills
  //     and removes the source row entirely. Destructive; ask before
  //     doing it. Note: a backend restart will re-seed the default
  //     source if it was hard-deleted (the seed re-runs), so this
  //     action is best for "I want this catalog out of my way
  //     until the next deploy" rather than "I never want this".
  // For non-default (user-added) sources there's no Hide option —
  // the user added it, they can only delete it.
  // ``options.action`` is "hide" or "delete" (default "delete") and
  // comes from the MarketplaceTab button the user clicked.
  //
  // User feedback (2026-07-29): "delete button is not working" — turned
  // out the issue was twofold:
  //   1. The default ``window.confirm`` dialog can be blocked by some
  //      embedded webviews / browser settings, so clicking Delete
  //      appeared to do nothing. Replaced with the in-app toast-based
  //      confirmation so the user always gets feedback.
  //   2. Network errors were swallowed silently with only a
  //      ``console.error``. Now we surface the error to the toast so
  //      the user knows what went wrong.
  const [deletingSourceId, setDeletingSourceId] = useState(null);
  const [pendingConfirm, setPendingConfirm] = useState(null);
  // ``pendingConfirm`` is the confirm dialog payload — null = no
  // dialog open, otherwise an object with the source + action +
  // message. Renders an in-app modal so the user always sees a
  // confirmation (unlike window.confirm which can be blocked).
  const handleMarketDeleteSource = (source, options = {}) => {
    const action = options.action || 'delete';
    if (source.is_default && action === 'hide') {
      setPendingConfirm({
        source,
        action: 'hide',
        title: `Hide "${source.name}"?`,
        message: 'This source will be removed from the marketplace tab. You can re-show it later — no skills are deleted.',
        confirmLabel: 'Hide',
        destructive: false,
      });
      return;
    }
    const forceHardDelete = source.is_default && action === 'delete';
    setPendingConfirm({
      source,
      action: 'delete',
      title: forceHardDelete
        ? `PERMANENTLY DELETE "${source.name}"?`
        : `Delete "${source.name}"?`,
      message: forceHardDelete
        ? `This removes the source AND all ${source.skill_count || 0} skills from this source. The source may reappear on the next backend restart if it is a curated catalog.`
        : `All ${source.skill_count || 0} skills from this source will be removed.`,
      confirmLabel: 'Delete',
      destructive: true,
    });
  };

  const confirmMarketDelete = async () => {
    if (!pendingConfirm) return;
    const { source, action } = pendingConfirm;
    const forceHardDelete = source.is_default && action === 'delete';
    setPendingConfirm(null);
    setDeletingSourceId(source.id);
    // Compute the next active source OUTSIDE the setState callback.
    // We can't just call setActiveSourceId inside the setMarketSources
    // updater — that's the classic "setState in setState" anti-pattern
    // that React 18+ warns about. Reading the captured closure value of
    // ``activeSourceId`` here is fine because the click is synchronous
    // up to the await; the user can't change the active source while
    // the request is in flight (the buttons are disabled).
    const wasActive = activeSourceId === source.id;
    try {
      const result = await deleteSource(source.id, { force: forceHardDelete });
      setMarketSources((prev) => {
        const next = prev.filter((s) => s.id !== source.id);
        if (wasActive) {
          // Fall back to the first remaining source so the grid
          // doesn't go blank. Schedule via queueMicrotask to keep
          // this updater pure (React tolerates the
          // setState-inside-updater warning, but doing it cleanly
          // avoids the warning entirely).
          const nextId = next[0]?.id || null;
          setActiveSourceId(nextId);
        }
        return next;
      });
      const verb = action === 'hide' ? 'Hidden' : 'Deleted';
      // If the user hard-deleted a default source, the backend just
      // added a tombstone to ``removed_curated_urls``. Make sure the
      // removed-sources panel stays in sync.
      if (action === 'delete' && source.is_default) {
        setRemovedSources((prev) => {
          if (prev.some((r) => r.url === source.url)) return prev;
          return [
            { url: source.url, removed_at: new Date().toISOString(), removed_by: null },
            ...prev,
          ];
        });
      }
      toast({
        title: `${verb} "${source.name}"`,
        description: action === 'delete'
          ? source.is_default
            ? `Removed source and ${source.skill_count || 0} skill(s). The URL is tombstoned so the seed won't re-create it. Click "Show removed" to restore.`
            : `Removed source and ${source.skill_count || 0} skill(s).`
          : 'The source is hidden. Use "Show" to bring it back.',
        variant: 'default',
        duration: 8000,
      });
      console.log('[marketplace] source', action, 'succeeded:', result);
    } catch (e) {
      console.error('[marketplace] source', action, 'failed:', e);
      toast({
        title: `Failed to ${action} "${source.name}"`,
        description: e.message || 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setDeletingSourceId(null);
    }
  };

  const handleMarketSourceAdded = (newSource) => {
    setMarketSources((prev) => [...prev, newSource]);
    setActiveSourceId(newSource.id);
    setAddSourceOpen(false);
  };

  const activeSource = marketSources.find((s) => s.id === activeSourceId) || marketSources[0] || null;

  const FILTERS = [
    { key: 'all', label: t.toolkit.filterAll },
    { key: 'official', label: t.toolkit.filterOfficial },
    { key: 'community', label: t.toolkit.filterCommunity },
  ];

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <div className="mb-6">
        <h1 className="font-display text-3xl tracking-tight text-foreground">{t.toolkit.skillsTitle}</h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{t.toolkit.skillsSubtitle}</p>
      </div>

      <div className="mb-6 flex flex-wrap items-center gap-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90">
              <Plus className="h-4 w-4" /> {t.toolkit.create} <ChevronDown className="h-3.5 w-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-64">
            <DropdownMenuItem onClick={openBuildWithAgent} className="py-2.5">
              <Sparkles className="mr-2 h-4 w-4 shrink-0 text-primary" />
              <div>
                <div className="text-sm font-medium">{t.toolkit.buildWithAgent}</div>
                <div className="text-[11px] text-muted-foreground">{t.toolkit.buildWithAgentDesc}</div>
              </div>
            </DropdownMenuItem>
            <DropdownMenuItem onClick={openUploadSkill} className="py-2.5">
              <UploadCloud className="mr-2 h-4 w-4 shrink-0 text-primary" />
              <div>
                <div className="text-sm font-medium">{t.toolkit.uploadSkill}</div>
                <div className="text-[11px] text-muted-foreground">{t.toolkit.uploadSkillDesc}</div>
              </div>
            </DropdownMenuItem>
            <DropdownMenuItem onClick={openGithubCollect} className="py-2.5">
              <Globe className="mr-2 h-4 w-4 shrink-0 text-primary" />
              <div>
                <div className="text-sm font-medium">{t.toolkit.addFromGithub}</div>
                <div className="text-[11px] text-muted-foreground">{t.toolkit.addFromGithubDesc}</div>
              </div>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <button onClick={() => setMySkillsOpen(true)} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground">
          <User className="h-4 w-4" /> {t.toolkit.mySkills}
        </button>
      </div>

      <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
        <div className="flex flex-wrap gap-1">
          <button onClick={() => setMainTab('skills')} className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors ${mainTab === 'skills' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>
            {t.toolkit.skillsTab}
          </button>
          <button onClick={() => setMainTab('mcp')} className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors ${mainTab === 'mcp' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>
            <Plug className="h-4 w-4" /> {t.toolkit.mcpServers}
          </button>
          <button onClick={() => setMainTab('marketplace')} className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors ${mainTab === 'marketplace' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>
            <Globe className="h-4 w-4" /> Browse Marketplace
          </button>
        </div>
        {mainTab === 'skills' && (
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex gap-1 rounded-lg bg-secondary/50 p-0.5">
              {FILTERS.map((f) => (
                <button key={f.key} onClick={() => setFilter(f.key)} className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${filter === f.key ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
                  {f.label}
                </button>
              ))}
            </div>
            <div className="relative w-52">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t.toolkit.searchPlaceholder} className="w-full rounded-lg border border-border bg-card pl-9 pr-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary/30" />
            </div>
            <select value={sort} onChange={(e) => setSort(e.target.value)} className="rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary/30">
              <option value="popular">{t.toolkit.sortPopular}</option>
              <option value="name">{t.toolkit.sortName}</option>
              <option value="newest">{t.toolkit.sortNewest}</option>
            </select>
          </div>
        )}
      </div>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : mainTab === 'mcp' ? (
        <McpSection t={t} />
      ) : mainTab === 'marketplace' ? (
        <div>
          {marketSourcesError && (
            <div className="mb-3 flex items-center gap-2 rounded-lg bg-red-500/10 px-3 py-2">
              <AlertCircle className="h-4 w-4 text-red-400" />
              <span className="text-xs text-red-300">{marketSourcesError}</span>
            </div>
          )}

          {/* Source cards row — always visible when the marketplace tab
              is active, horizontally scrollable on narrow viewports. The
              trailing "+" card opens the AddMarketplaceDialog. */}
          <div className="-mx-1 mb-3 flex gap-3 overflow-x-auto px-1 pb-1">
            {marketSourcesLoading && marketSources.length === 0 ? (
              <div className="flex h-[56px] items-center gap-2 px-2 text-xs text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading sources…
              </div>
            ) : (
              <>
                {marketSources.map((src) => (
                  <SourceCard
                    key={src.id}
                    source={src}
                    isActive={src.id === activeSourceId}
                    onClick={(s) => setActiveSourceId(s.id)}
                  />
                ))}
                <SourceCard
                  kind="add"
                  onAdd={() => setAddSourceOpen(true)}
                  addLabel="Add source"
                />
              </>
            )}
          </div>

          {/* "Show removed" toggle. Surfaces any curated sources the
              user has hard-deleted; clicking opens a panel where they
              can click "Restore" to bring a source back. Solves the
              user complaint on 2026-07-29: "after refresh it showing
              again it's not working how i want it" — the user wanted
              deletes to persist, and they need a way to undo. */}
          {removedSources.length > 0 && (
            <div className="mb-4">
              <button
                onClick={() => setRemovedPanelOpen((v) => !v)}
                className="inline-flex items-center gap-1.5 rounded-md border border-dashed border-border px-2.5 py-1 text-xs text-muted-foreground hover:border-foreground/30 hover:text-foreground"
              >
                {removedPanelOpen ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                {removedPanelOpen ? 'Hide' : 'Show'} removed ({removedSources.length})
              </button>
              {removedPanelOpen && (
                <div className="mt-2 space-y-1.5 rounded-md border border-dashed border-border bg-card/40 p-2.5">
                  {removedSources.map((r) => (
                    <div key={r.url} className="flex items-center justify-between gap-2 text-xs">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-foreground">{r.url}</div>
                        <div className="text-[10px] text-muted-foreground">
                          Removed {r.removed_at ? formatAbsoluteTime(r.removed_at) : 'recently'}
                        </div>
                      </div>
                      <button
                        onClick={() => handleRestoreRemoved(r.url)}
                        disabled={restoringUrl === r.url}
                        className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:border-foreground/40 hover:text-foreground disabled:opacity-50"
                      >
                        {restoringUrl === r.url ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <Undo2 className="h-3 w-3" />
                        )}
                        Restore
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {activeSource ? (
            <MarketplaceTab
              source={activeSource}
              installedNames={installedMarketNames}
              onInstall={handleMarketInstall}
              onOpenDetail={setDetailSkill}
              onDelete={handleMarketDeleteSource}
              busy={deletingSourceId === activeSource?.id}
            />
          ) : !marketSourcesLoading ? (
            <div className="rounded-xl border border-dashed border-border bg-card/40 p-8 text-center">
              <p className="text-sm text-muted-foreground">No sources yet. Click "+ Add source" to register a GitHub repo, JSON index, or web page.</p>
            </div>
          ) : null}
        </div>
      ) : (
        <SkillCardGrid items={filtered} t={t} onOpen={(id) => navigate(`/toolkit/${id}`)} onAdd={addToMySkills} addedNames={mySkillNames} addingNames={adding} isMySkills={false} />
      )}

      <MySkillsModal open={mySkillsOpen} onOpenChange={setMySkillsOpen} items={mySkills} t={t} onOpen={(id) => navigate(`/toolkit/${id}`)} onEdit={openEditSkill} onDelete={deleteSkill} />
      <SkillUploadDialog open={uploadOpen} onOpenChange={setUploadOpen} kind="system_skill" onSaved={load} initialMode={uploadMode} />
      <AddMarketplaceDialog open={addSourceOpen} onClose={() => setAddSourceOpen(false)} onAdded={handleMarketSourceAdded} />
      <SkillDetailSheet
        skill={detailSkill}
        open={!!detailSkill}
        onClose={() => setDetailSkill(null)}
        onInstall={(sk) => { handleMarketInstall(sk); setDetailSkill(null); }}
        isInstalled={detailSkill ? installedMarketNames.has(detailSkill.name) : false}
      />
      <ConfirmDialog
        open={!!pendingConfirm}
        title={pendingConfirm?.title}
        message={pendingConfirm?.message}
        confirmLabel={pendingConfirm?.confirmLabel}
        destructive={pendingConfirm?.destructive}
        busy={!!deletingSourceId}
        onConfirm={confirmMarketDelete}
        onCancel={() => setPendingConfirm(null)}
      />
    </div>
  );
}
