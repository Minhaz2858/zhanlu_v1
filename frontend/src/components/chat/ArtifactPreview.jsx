import { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { Loader2, Download, Network, Cpu, Wrench } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import FilePreviewer from './FilePreviewer';
import HtmlArtifactPreview from './HtmlArtifactPreview';
import DashboardArtifactPreview from './DashboardArtifactPreview';

const ENTITY_MAP = {
  report: 'Report',
  file: 'UserFile',
  agent: 'AgentApp',
  kb: 'KnowledgeBase',
  automation: 'AutomationTask',
  flow: 'DecisionFlow',
};

const md = {
  p: (p) => <p className="mb-2 last:mb-0 leading-relaxed text-sm" {...p} />,
  ul: (p) => <ul className="mb-2 list-disc space-y-1 pl-4 text-sm" {...p} />,
  ol: (p) => <ol className="mb-2 list-decimal space-y-1 pl-4 text-sm" {...p} />,
  li: (p) => <li className="text-sm" {...p} />,
  code: (p) => <code className="rounded bg-secondary px-1.5 py-0.5 font-mono text-xs" {...p} />,
  pre: (p) => <pre className="mb-2 overflow-x-auto rounded-lg bg-secondary p-3 text-xs" {...p} />,
  h1: (p) => <h1 className="mb-2 font-display text-lg" {...p} />,
  h2: (p) => <h2 className="mb-2 font-display text-base" {...p} />,
  h3: (p) => <h3 className="mb-1 font-display text-sm font-semibold" {...p} />,
};

function extOf(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '');
  return m ? m[1].toLowerCase() : '';
}

function Meta({ rows }) {
  return (
    <div className="rounded-lg border border-border bg-background p-4">
      <dl className="space-y-2 text-sm">
        {rows.filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== '').map(([k, v]) => (
          <div key={k} className="flex gap-2">
            <dt className="w-24 shrink-0 text-xs text-muted-foreground">{k}</dt>
            <dd className="flex-1 text-foreground">{String(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Tag({ children }) {
  return <span className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[11px] text-foreground">{children}</span>;
}

export default function ArtifactPreview({ result }) {
  const { t } = useLanguage();
  const entity = ENTITY_MAP[result.type];
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const isDraft = result.draft && !result.id;

  useEffect(() => {
    let active = true;
    if (isDraft || !entity || !result.id) { setLoading(false); return; }
    base44.entities[entity].get(result.id)
      .then((d) => { if (active) setData(d); })
      .catch((e) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [result.id, entity, isDraft]);

  if (isDraft) return <DraftPreview result={result} t={t} />;

  if (!entity) return null;
  if (loading) return (
    <div className="flex items-center gap-2 py-6 text-xs text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" /> {t.chat.thinking}
    </div>
  );
  if (error || !data) return <p className="py-3 text-xs text-muted-foreground">{t.detail.notFound}</p>;

  // PPTX: render slides inline when slide data is available
  if (result.type === 'file' && data.file_type === 'pptx') {
    const slides = Array.isArray(data.slides) ? data.slides : [];
    if (slides.length > 0) return <SlidePreview slides={slides} />;
  }

  // HTML artifacts: rendered preview and source code in one panel
  if (result.type === 'file' && ['html', 'htm'].includes((data.file_type || extOf(data.name)).toLowerCase()) && data.file_url) {
    return <HtmlArtifactPreview url={data.file_url} />;
  }

  // File: preview by extension (images, pdf, office docs, dashboards, webapps)
  if (result.type === 'file' && data.file_url) {
    return <FilePreviewer url={data.file_url} name={data.name} kind={data.resource_kind} className="h-[560px] w-full rounded-lg border border-border bg-card" />;
  }

  // Dashboard reports: render a dashboard canvas rather than plain report text
  if (result.type === 'report' && /dashboard|仪表盘/i.test(`${data.title || result.name || ''} ${data.type || ''}`)) {
    return <DashboardArtifactPreview title={data.title || result.name} summary={data.summary} status={t.detail.reportStatuses[data.status] || data.status} />;
  }

  // Report: render summary markdown inline (DOCX still available via download)
  if (result.type === 'report') {
    const content = data.summary || '';
    return (
      <div className="space-y-3">
        {content.trim() ? (
          <div className="rounded-lg border border-border bg-background p-4">
            <ReactMarkdown components={md}>{content}</ReactMarkdown>
          </div>
        ) : (
          <Meta rows={[[t.detail.summary, '—'], [t.automation.status, t.detail.reportStatuses[data.status] || data.status]]} />
        )}
      </div>
    );
  }

  // Agent: key config snapshot
  if (result.type === 'agent') {
    const layerKeys = [
      ['prompt_identity', t.agentConfig.layers.identity],
      ['prompt_boundary', t.agentConfig.layers.boundary],
      ['prompt_reasoning', t.agentConfig.layers.reasoning],
      ['prompt_tools', t.agentConfig.layers.tools],
      ['prompt_output', t.agentConfig.layers.output],
    ];
    const first = layerKeys.find(([k]) => data[k] && String(data[k]).trim());
    return (
      <div className="space-y-2 rounded-lg border border-border bg-background p-4">
        {data.description && <p className="text-sm text-muted-foreground">{data.description}</p>}
        <div className="flex flex-wrap gap-1.5">
          {data.model && <Tag><Cpu className="h-3 w-3" /> {data.model}</Tag>}
          {data.topology && <Tag><Network className="h-3 w-3" /> {t.agentConfig.topologies[data.topology]}</Tag>}
          {(data.skills || []).length > 0 && <Tag><Wrench className="h-3 w-3" /> {data.skills.length} {t.agentConfig.items}</Tag>}
        </div>
        {first && (
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">{first[1]}</p>
            <div className="whitespace-pre-wrap rounded-lg bg-secondary/50 p-3 text-xs leading-relaxed">{data[first[0]]}</div>
          </div>
        )}
      </div>
    );
  }

  // kb / automation / flow: metadata
  const rows = [];
  if (data.description) rows.push([t.agentConfig.description, data.description]);
  if (result.type === 'kb') {
    rows.push([t.detail.kbType, t.detail.kbTypes[data.type] || data.type]);
    rows.push([t.detail.itemCount, data.item_count]);
  }
  if (result.type === 'automation') {
    rows.push([t.automation.cols.type, t.automation.types[data.type] || data.type]);
    rows.push([t.automation.schedule, data.schedule || '—']);
    rows.push([t.automation.status, t.automation.statuses[data.status] || data.status]);
  }
  if (result.type === 'flow') {
    rows.push([t.detail.steps, data.steps]);
    rows.push([t.automation.status, t.detail.flowStatuses[data.status] || data.status]);
  }
  return <Meta rows={rows} />;
}

// Draft preview: render directly from result.fields without fetching entity
function DraftPreview({ result, t }) {
  const f = result.fields || {};

  if (result.type === 'report' && /dashboard|仪表盘/i.test(`${result.name || ''} ${f.type || ''}`)) {
    return <DashboardArtifactPreview title={result.name} summary={f.summary} status={t.detail.reportStatuses[f.status] || f.status} />;
  }

  if (result.type === 'report') {
    const content = f.summary || '';
    if (!content.trim()) return <Meta rows={[[t.detail.summary, '—']]} />;
    return (
      <div className="rounded-lg border border-border bg-background p-4">
        <ReactMarkdown components={md}>{content}</ReactMarkdown>
      </div>
    );
  }

  if (result.type === 'agent') {
    const layerKeys = [
      ['prompt_identity', t.agentConfig.layers.identity],
      ['prompt_boundary', t.agentConfig.layers.boundary],
      ['prompt_reasoning', t.agentConfig.layers.reasoning],
      ['prompt_tools', t.agentConfig.layers.tools],
      ['prompt_output', t.agentConfig.layers.output],
    ];
    const first = layerKeys.find(([k]) => f[k] && String(f[k]).trim());
    return (
      <div className="space-y-2 rounded-lg border border-border bg-background p-4">
        {f.description && <p className="text-sm text-muted-foreground">{f.description}</p>}
        <div className="flex flex-wrap gap-1.5">
          {f.model && <Tag><Cpu className="h-3 w-3" /> {f.model}</Tag>}
          {f.topology && <Tag><Network className="h-3 w-3" /> {t.agentConfig.topologies[f.topology]}</Tag>}
          {(f.skills || []).length > 0 && <Tag><Wrench className="h-3 w-3" /> {f.skills.length} {t.agentConfig.items}</Tag>}
        </div>
        {first && (
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">{first[1]}</p>
            <div className="whitespace-pre-wrap rounded-lg bg-secondary/50 p-3 text-xs leading-relaxed">{f[first[0]]}</div>
          </div>
        )}
      </div>
    );
  }

  const rows = [];
  if (f.description) rows.push([t.agentConfig.description, f.description]);
  if (result.type === 'kb') {
    rows.push([t.detail.kbType, t.detail.kbTypes[f.type] || f.type]);
  }
  if (result.type === 'automation') {
    rows.push([t.automation.cols.type, t.automation.types[f.type] || f.type]);
    rows.push([t.automation.schedule, f.schedule || '—']);
  }
  if (result.type === 'flow') {
    rows.push([t.detail.steps, f.steps]);
  }
  if (result.type === 'file' && ['html', 'htm'].includes((f.file_type || '').toLowerCase()) && f.html_content) {
    return <HtmlArtifactPreview content={f.html_content} />;
  }
  if (result.type === 'file') {
    if (f.file_type) rows.push([t.detail.fileType, f.file_type]);
    rows.push([t.detail.source, t.detail.fileSources[f.source] || f.source || '—']);
  }
  if (result.type === 'file' && f.file_type === 'pptx' && Array.isArray(f.slides) && f.slides.length > 0) {
    return <SlidePreview slides={f.slides} />;
  }

  if (rows.length === 0) rows.push([t.detail.summary, '—']);
  return <Meta rows={rows} />;
}

function SlidePreview({ slides }) {
  return (
    <div className="space-y-3">
      {slides.map((slide, i) => (
        <div key={i} className="rounded-lg border border-border bg-background p-4">
          {slide.title && (
            <p className="mb-2 font-display text-sm font-semibold text-foreground">{i + 1}. {slide.title}</p>
          )}
          {Array.isArray(slide.bullets) && slide.bullets.length > 0 && (
            <ul className="space-y-1 pl-4">
              {slide.bullets.map((b, j) => (
                <li key={j} className="text-xs text-muted-foreground">• {b}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}