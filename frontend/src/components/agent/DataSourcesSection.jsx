import { useState, useEffect } from 'react';
import { Database, FileText, Plus, X, Search, Loader2 } from 'lucide-react';
import { Section } from './AgentParts';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';

export default function DataSourcesSection({ form, update, onChange, t }) {
  const { t: tr } = useLanguage();
  const [available, setAvailable] = useState([]);
  const [allKbs, setAllKbs] = useState([]);
  const [showPicker, setShowPicker] = useState(false);
  const [query, setQuery] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [loading, setLoading] = useState(false);

  async function loadKbs() {
    setLoading(true);
    setLoadError(null);
    try {
      const kbs = await base44.entities.KnowledgeBase.list();
      setAllKbs(kbs || []);
    } catch (err) {
      console.error('Failed to load data sources:', err);
      setLoadError(err?.message || 'Failed to load data sources');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadKbs();
  }, [form.project]);

  // Refresh the KB list every time the picker opens so newly created
  // databases appear without requiring a page reload.
  useEffect(() => {
    if (showPicker) loadKbs();
  }, [showPicker]);

  // Show every KnowledgeBase from My Space → Connectors — both database
  // connections and uploaded document KBs. The agent runtime
  // (data_source_runtime.py) handles both source_kinds transparently:
  // bound file KBs enable semantic search / answer_from_documents, bound
  // database KBs enable SQL execution — both surface via ask_data_agent.
  //
  // Project scoping is kept loose on purpose:
  //   - Global KBs are visible to every agent.
  //   - Project-scoped KBs are visible when the agent's project is
  //     unset, "global", or matches the KB's project.
  // The previous filter dropped DBs whenever the agent's project was a
  // placeholder value (e.g. "Ungrouped") that did not match the KB's
  // project, which made it look like a connected database had disappeared.
  //
  // An earlier version of this filter ALSO dropped every source_kind
  // except 'database', which silently hid file KBs from the picker even
  // though they appeared in My Space → Connectors and were valid bound
  // data sources per the runtime. The fix removes that gate so the
  // agent-side list mirrors the connector catalog one-to-one.
  useEffect(() => {
    const agentProject = (form.project || 'global').trim();
    const isUngrouped = !agentProject || agentProject === 'global' || agentProject === 'Ungrouped' || agentProject === '未分组';
    const next = allKbs.filter((kb) => {
      // Only KnowledgeBase rows with a known source_kind are bindable.
      // Rows without source_kind (legacy / malformed) are hidden so we
      // don't bind something the runtime can't dispatch on.
      const kind = kb.source_kind;
      if (kind !== 'database' && kind !== 'file') return false;
      const kbProject = (kb.project || '').trim();
      if (!kbProject || kbProject === 'global') return true;
      if (isUngrouped) return true;
      return kbProject === agentProject;
    });
    setAvailable(next);
  }, [allKbs, form.project]);

  const selected = form.knowledge_bases || [];
  const filtered = available.filter(
    (kb) =>
      !selected.includes(kb.id) &&
      (kb.name || '').toLowerCase().includes(query.toLowerCase())
  );

  async function persistKnowledgeBases(newList) {
    // Always sync local form state first so the UI feels instant.
    update({ knowledge_bases: newList });
    if (!onChange) return;
    setSaving(true);
    setSaveError(null);
    try {
      await onChange({ knowledge_bases: newList });
    } catch (err) {
      console.error('Failed to persist data source change:', err);
      setSaveError(t.agentConfig?.saveError || 'Save failed');
    } finally {
      setSaving(false);
    }
  }

  function addKb(id) {
    if (id && !selected.includes(id)) {
      persistKnowledgeBases([...selected, id]);
    }
    setShowPicker(false);
    setQuery('');
  }
  function removeKb(id) {
    persistKnowledgeBases(selected.filter((x) => x !== id));
  }

  return (
    <Section title={t.agentConfig.dataSources} desc={t.agentConfig.dataSourcesDesc} icon={Database}>
      <div className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {selected.map((id) => {
            const kb = available.find((x) => x.id === id);
            const label = kb?.name || id;
            // Database KBs carry db_type; file KBs carry file_type. Pick
            // the right one so the chip shows "MySQL" / "PostgreSQL" for
            // databases and "PDF" / "DOCX" for documents.
            const isFileKb = kb?.source_kind === 'file';
            const rawTypeLabel = isFileKb
              ? (tr.kb.fileTypes?.[kb.file_type] || kb.file_type || '')
              : (tr.kb.dbTypes?.[kb.db_type] || kb.db_type || '');
            const Icon = isFileKb ? FileText : Database;
            return (
              <span key={id} className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-secondary/50 px-2.5 py-1.5 text-xs text-foreground">
                <Icon className="h-3 w-3 text-primary" />
                <span>{label}</span>
                {rawTypeLabel && <span className="rounded bg-primary/10 px-1 py-0.5 text-[10px] text-primary">{rawTypeLabel}</span>}
                <button onClick={() => removeKb(id)} className="text-muted-foreground hover:text-destructive"><X className="h-3 w-3" /></button>
              </span>
            );
          })}
          {selected.length === 0 && <span className="text-xs text-muted-foreground">{t.agentConfig.noDataSources}</span>}
        </div>

        {saving && (
          <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" /> {t.agentConfig?.saving || 'Saving…'}
          </div>
        )}
        {saveError && (
          <div className="flex items-center gap-1.5 text-[10px] text-destructive">
            {saveError}
          </div>
        )}

        {showPicker ? (
          <div className="rounded-lg border border-border bg-background p-2">
            <div className="mb-2 flex items-center gap-2">
              <Search className="h-3.5 w-3.5 text-muted-foreground" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t.agentConfig.searchDataSources}
                className="flex-1 bg-transparent text-xs focus:outline-none"
                autoFocus
              />
              <button onClick={() => setShowPicker(false)} className="text-muted-foreground hover:text-foreground"><X className="h-3.5 w-3.5" /></button>
            </div>
            <div className="max-h-48 space-y-1 overflow-y-auto">
              {loading ? (
                <p className="flex items-center justify-center gap-1.5 px-2 py-3 text-center text-xs text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" /> {t.common?.loading || 'Loading…'}
                </p>
              ) : loadError ? (
                <p className="px-2 py-3 text-center text-xs text-destructive">{loadError}</p>
              ) : filtered.length === 0 ? (
                <div className="px-2 py-3 text-center text-xs text-muted-foreground">
                  <p>{t.agentConfig.noAvailableDataSources}</p>
                  {allKbs.length > 0 && (
                    <p className="mt-1 text-[10px]">
                      {allKbs.filter((k) => k.source_kind === 'database' || k.source_kind === 'file').length} data source(s) exist but are filtered by project scope.
                    </p>
                  )}
                </div>
              ) : (
                filtered.map((kb) => {
                  const isFileKb = kb.source_kind === 'file';
                  const Icon = isFileKb ? FileText : Database;
                  const typeLabel = isFileKb
                    ? (tr.kb.fileTypes?.[kb.file_type] || kb.file_type || '')
                    : (tr.kb.dbTypes?.[kb.db_type] || kb.db_type || '');
                  return (
                    <button
                      key={kb.id}
                      onClick={() => addKb(kb.id)}
                      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-secondary"
                    >
                      <Plus className="h-3 w-3 text-muted-foreground" />
                      <Icon className="h-3 w-3 text-primary" />
                      <span className="flex-1 text-foreground">{kb.name}</span>
                      <span className="text-[10px] text-muted-foreground">{typeLabel}</span>
                    </button>
                  );
                })
              )}
            </div>
          </div>
        ) : (
          <button onClick={() => setShowPicker(true)} className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-border px-3 py-1.5 text-xs text-muted-foreground hover:border-primary/40 hover:text-foreground">
            <Plus className="h-3.5 w-3.5" /> {t.agentConfig.addDataSource}
          </button>
        )}
      </div>
    </Section>
  );
}