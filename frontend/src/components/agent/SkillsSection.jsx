import { useState, useEffect, useRef } from 'react';
import { Wrench, Plus, X, Search, Store, Loader2, Package } from 'lucide-react';
import { Section } from './AgentParts';
import { base44 } from '@/api/base44Client';
import { getAnonymousId } from '@/lib/anonymousId';
import { authFetch } from '@/api/authFetch';
import { coerceStringArray } from '@/lib/jsonArray';

export default function SkillsSection({ form, update, t }) {
  const [userSkills, setUserSkills] = useState([]);
  const [searchResults, setSearchResults] = useState([]);
  const [showPicker, setShowPicker] = useState(false);
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [activeTab, setActiveTab] = useState('all'); // 'all' | 'marketplace' | 'mine'
  const searchTimer = useRef(null);

  // The skills column on AgentApp stores string[] in JSON, but legacy
  // rows can come back as a bare string from the API. Normalize once
  // here so .map/.filter/.length/.includes never crash on a string.
  const skills = coerceStringArray(form?.skills);

  // Load user's DB skills once. "User" is the real account when logged in
  // and the SDK's per-browser anonymous id otherwise — the same value
  // the backend stamps on rows created from this browser, so the filter
  // finds what this browser actually owns. Without this fallback, an
  // anonymous browser's `+` clicks on /toolkit would never show up in
  // the "My Skills" tab here.
  useEffect(() => {
    Promise.all([base44.entities.Tool.list(), base44.auth.me().catch(() => null)])
      .then(([tools, u]) => {
        const ownerId = u?.id || getAnonymousId();
        setUserSkills(ownerId ? tools.filter((x) => x.created_by_id === ownerId) : []);
      })
      .catch(() => {});
  }, []);

  // Debounced unified search when picker is open and query changes
  useEffect(() => {
    if (!showPicker) return;
    if (searchTimer.current) clearTimeout(searchTimer.current);
    if (!query.trim()) {
      setSearchResults([]);
      return;
    }
    setSearching(true);
    searchTimer.current = setTimeout(async () => {
      try {
        const res = await authFetch(`/api/skills/unified-search?q=${encodeURIComponent(query)}&limit=20`);
        if (res.ok) {
          const data = await res.json();
          setSearchResults(data || []);
        } else {
          setSearchResults([]);
        }
      } catch {
        setSearchResults([]);
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => { if (searchTimer.current) clearTimeout(searchTimer.current); };
  }, [query, showPicker]);

  // Combine results: user DB skills (filtered) + unified search results
  const allResults = [
    // User's DB skills (always visible, even without search)
    ...userSkills
      .filter((tool) => !skills.includes(tool.name))
      .filter((tool) => !query || tool.name.toLowerCase().includes(query.toLowerCase()))
      .map((tool) => ({
        name: tool.name,
        description: tool.description || '',
        category: tool.category || '',
        trigger: tool.trigger || '',
        source: 'db',
        kind: tool.kind || 'custom_tool',
        id: tool.id,
      })),
    // Unified search results (includes marketplace filesystem skills)
    ...searchResults
      .filter((r) => !skills.includes(r.name))
      .filter((r) => activeTab === 'all' || (activeTab === 'marketplace' && r.source === 'filesystem') || (activeTab === 'mine' && r.source === 'db'))
      .filter((r) => !userSkills.find((u) => u.name === r.name)), // dedupe
  ];

  function addSkill(name) {
    if (name && !skills.includes(name)) update({ skills: [...skills, name] });
    setShowPicker(false);
    setQuery('');
    setSearchResults([]);
  }
  function removeSkill(name) {
    update({ skills: skills.filter((s) => s !== name) });
  }

  const tabs = [
    { key: 'all', label: t.agentConfig.allSkills || 'All' },
    { key: 'marketplace', label: t.agentConfig.marketplace },
    { key: 'mine', label: t.agentConfig.mySkills || 'Mine' },
  ];

  return (
    <Section title={t.agentConfig.skills} desc={t.agentConfig.skillsDesc} icon={Wrench}>
      <div className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {skills.map((s) => {
            const tool = userSkills.find((x) => x.name === s);
            const isMarketplace = !tool;
            return (
              <span key={s} className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-2.5 py-1.5 text-xs text-foreground">
                {isMarketplace ? <Store className="h-3 w-3 shrink-0 text-amber-600" /> : <Wrench className="h-3 w-3 shrink-0 text-primary" />}
                <span className="min-w-0 break-all">{s}</span>
                {tool?.kind === 'system_skill' && (
                  <span className="rounded bg-primary/10 px-1 py-0.5 text-[10px] text-primary">{t.agentConfig.native}</span>
                )}
                {isMarketplace && (
                  <span className="rounded bg-amber-500/10 px-1 py-0.5 text-[10px] text-amber-600">{t.agentConfig.marketplace}</span>
                )}
                <button onClick={() => removeSkill(s)} className="text-muted-foreground hover:text-destructive"><X className="h-3 w-3" /></button>
              </span>
            );
          })}
          {skills.length === 0 && <span className="text-xs text-muted-foreground">{t.agentConfig.noSkills}</span>}
        </div>

        {showPicker ? (
          <div className="w-full min-w-0 overflow-hidden rounded-lg border border-border bg-background p-2">
            <div className="mb-2 flex items-center gap-2">
              <Search className="h-3.5 w-3.5 text-muted-foreground" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t.agentConfig.searchSkill || 'Search skills...'}
                className="flex-1 bg-transparent text-xs focus:outline-none"
                autoFocus
              />
              {searching && <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />}
              <button onClick={() => setShowPicker(false)} className="text-muted-foreground hover:text-foreground"><X className="h-3.5 w-3.5" /></button>
            </div>

            {/* Tab filter */}
            <div className="mb-2 flex gap-1">
              {tabs.map((tab) => (
                <button
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  className={`rounded-md px-2 py-1 text-[11px] transition-colors ${activeTab === tab.key ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'}`}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            <div className="max-h-56 space-y-1 overflow-x-hidden overflow-y-auto">
              {allResults.length === 0 && !searching ? (
                <p className="px-2 py-3 text-center text-xs text-muted-foreground">
                  {query ? (t.agentConfig.noAvailableSkills || 'No skills found') : (t.agentConfig.searchSkill || 'Type to search marketplace skills...')}
                </p>
              ) : (
                allResults.map((skill, i) => (
                  <button
                    key={skill.id || `${skill.name}-${i}`}
                    onClick={() => addSkill(skill.name)}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-secondary"
                  >
                    <Plus className="h-3 w-3 shrink-0 text-muted-foreground" />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1">
                        <span className="text-foreground truncate">{skill.name}</span>
                        {skill.source === 'filesystem' ? (
                          <Store className="h-2.5 w-2.5 shrink-0 text-amber-600" />
                        ) : (
                          <Package className="h-2.5 w-2.5 shrink-0 text-primary" />
                        )}
                      </div>
                      {skill.description && (
                        <p className="truncate text-[10px] text-muted-foreground">{skill.description}</p>
                      )}
                    </div>
                    {skill.category && (
                      <span className="shrink-0 rounded bg-secondary px-1 py-0.5 text-[10px] text-muted-foreground">{skill.category}</span>
                    )}
                  </button>
                ))
              )}
            </div>
          </div>
        ) : (
          <button onClick={() => setShowPicker(true)} className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-border px-3 py-1.5 text-xs text-muted-foreground hover:border-primary/40 hover:text-foreground">
            <Plus className="h-3.5 w-3.5" /> {t.agentConfig.addSkill}
          </button>
        )}
      </div>
    </Section>
  );
}
