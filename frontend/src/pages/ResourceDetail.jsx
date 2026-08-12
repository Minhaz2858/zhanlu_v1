import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import PageHeader from '@/components/PageHeader';
import KbSetupDialog from '@/components/kb/KbSetupDialog';
import KbSourceDetail from '@/components/kb/KbSourceDetail';
import KbCatalogTables from '@/components/kb/KbCatalogTables';
import { Loader2, Trash2, Tag, FileText, Database, Workflow, BarChart3, Bot, Download } from 'lucide-react';

const TYPE_MAP = {
  agent: { entity: 'AgentApp', icon: Bot, statusKey: 'agentStatuses' },
  kb: { entity: 'KnowledgeBase', icon: Database, statusKey: 'kbStatuses' },
  file: { entity: 'UserFile', icon: FileText, statusKey: null },
  flow: { entity: 'DecisionFlow', icon: Workflow, statusKey: 'flowStatuses' },
  report: { entity: 'Report', icon: BarChart3, statusKey: 'reportStatuses' },
};

import { formatShortDateTime } from '@/lib/time';

export default function ResourceDetail() {
  const { type, id } = useParams();
  const navigate = useNavigate();
  const { t, lang } = useLanguage();
  const [item, setItem] = useState(null);
  const [loading, setLoading] = useState(true);
  const [kbOpen, setKbOpen] = useState(false);
  const cfg = TYPE_MAP[type] || TYPE_MAP.agent;

  useEffect(() => { load(); }, [type, id]);
  async function load() {
    try { setItem(await base44.entities[cfg.entity].get(id)); }
    catch { setItem(null); }
    finally { setLoading(false); }
  }
  async function remove() {
    await base44.entities[cfg.entity].delete(id);
    navigate('/my-space');
  }
  async function togglePauseKb() {
    if (type !== 'kb' || !item) return;
    await base44.entities.KnowledgeBase.update(id, { status: item.status === 'paused' ? 'active' : 'paused' });
    load();
  }

  const name = item?.name || item?.title;
  const desc = item?.description || item?.summary;
  const translate = useTranslate([name, desc, ...((item?.capabilities || []).filter(Boolean))].filter(Boolean), lang);

  if (loading) return <div className="flex h-full items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>;
  if (!item) return <div className="px-8 py-8"><PageHeader title={t.detail.notFound} /></div>;

  const Icon = cfg.icon;
  const statusLabel = cfg.statusKey && item.status ? (t.detail[cfg.statusKey][item.status] || item.status) : null;

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader
        title={translate(name)}
        subtitle={t.mySpace.tabs[type]}
        action={
          <div className="flex gap-2">
            <button onClick={remove} className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-2 text-sm text-muted-foreground hover:text-destructive"><Trash2 className="h-3.5 w-3.5" /> {t.common.delete}</button>
          </div>
        }
      />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Section title={t.detail.basicInfo}>
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-secondary"><Icon className="h-5 w-5 text-primary" /></div>
              {statusLabel && <span className="rounded-full bg-secondary px-2.5 py-0.5 text-xs text-muted-foreground">{statusLabel}</span>}
            </div>
            {desc && <p className="text-sm text-foreground">{translate(desc)}</p>}
          </Section>

          {type === 'agent' && (
            <Section title={t.detail.capabilities}>
              {item.capabilities && item.capabilities.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {item.capabilities.map((c, i) => <span key={i} className="inline-flex items-center gap-1 rounded-md bg-secondary px-2.5 py-1 text-xs text-foreground"><Tag className="h-3 w-3 text-muted-foreground" /> {translate(c)}</span>)}
                </div>
              ) : <p className="text-sm text-muted-foreground">{t.detail.noCapabilities}</p>}
            </Section>
          )}

          {type === 'kb' && (
            <>
              <KbSourceDetail item={item} t={t} onEdit={() => setKbOpen(true)} onTogglePause={togglePauseKb} />
              {(item.source_kind === 'database' || ['mysql','postgres','postgresql'].includes((item.db_type||'').toLowerCase())) && (
                <KbCatalogTables kbId={item.id} dbType={item.db_type} kbName={item.name} />
              )}
            </>
          )}

          {type === 'file' && (
            <Section title={t.detail.metadata}>
              <div className="space-y-3 text-sm">
                <Row label={t.detail.fileType} value={item.file_type || '—'} />
                <Row label={t.detail.fileSize} value={`${((item.size || 0) / 1024).toFixed(1)} KB`} />
                <Row label={t.detail.source} value={t.detail.fileSources[item.source] || item.source} />
                {item.file_url && <a href={item.file_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sm text-primary hover:underline"><Download className="h-3.5 w-3.5" /> {t.detail.download}</a>}
              </div>
            </Section>
          )}

          {type === 'flow' && (
            <Section title={t.detail.metadata}>
              <div className="space-y-3 text-sm">
                <Row label={t.detail.steps} value={item.steps} />
              </div>
            </Section>
          )}
        </div>

        <div className="space-y-6">
          <Section title={t.detail.metadata}>
            <div className="space-y-3 text-sm">
              <Row label={t.detail.createdDate} value={formatShortDateTime(item.created_date)} />
              <Row label={t.detail.updatedDate} value={formatShortDateTime(item.updated_date)} />
              {item.model && <Row label={t.detail.model} value={item.model} />}
              <div className="flex items-center justify-between border-t border-border pt-3">
                <span className="text-muted-foreground">ID</span>
                <span className="font-mono text-xs text-muted-foreground">{item.id.slice(-8)}</span>
              </div>
            </div>
          </Section>
        </div>
      </div>
      {type === 'kb' && <KbSetupDialog open={kbOpen} onOpenChange={setKbOpen} editItem={item} onSaved={load} />}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <h3 className="mb-4 font-display text-base text-foreground">{title}</h3>
      {children}
    </div>
  );
}
function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-foreground">{value}</span>
    </div>
  );
}