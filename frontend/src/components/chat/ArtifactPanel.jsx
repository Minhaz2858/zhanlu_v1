import { useState } from 'react';
import { X, Save, CheckCircle2, Loader2, Download, AlertCircle } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import ArtifactPreview from './ArtifactPreview';

const ENTITY_MAP = {
  report: 'Report',
  file: 'UserFile',
  agent: 'AgentApp',
  kb: 'KnowledgeBase',
  automation: 'AutomationTask',
  flow: 'DecisionFlow',
};

export default function ArtifactPanel({ result, onClose, onSaved, sessionId }) {
  const { t } = useLanguage();

  // Guard against null/undefined result — parent may pass falsy during loading
  if (!result) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="text-center">
          <Loader2 className="mx-auto h-6 w-6 animate-spin text-muted-foreground" />
          <p className="mt-2 text-sm text-muted-foreground">{t.chat?.loading || 'Loading...'}</p>
        </div>
      </div>
    );
  }

  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(!result.draft);
  const [savedId, setSavedId] = useState(result.id);
  const [error, setError] = useState(null);
  const [fileUrl, setFileUrl] = useState(result.file_url || result.fields?.file_url || null);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const entityName = ENTITY_MAP[result.type];
      if (!entityName) throw new Error('Unknown type');
      let fields = { ...(result.fields || {}) };
      if (result.type === 'file' && ['html', 'htm'].includes((fields.file_type || '').toLowerCase()) && fields.html_content) {
        const fileName = /\.html?$/i.test(result.name) ? result.name : `${result.name}.html`;
        const uploaded = await base44.integrations.Core.UploadFile({ file: new File([fields.html_content], fileName, { type: 'text/html' }) });
        fields = { ...fields, file_url: uploaded.file_url, source: 'ai_generated', resource_kind: 'html_file' };
        delete fields.html_content;
        setFileUrl(uploaded.file_url);
      }
      const payload = result.type === 'report'
        ? { title: result.name, ...fields }
        : { name: result.name, ...fields, ...(result.type === 'file' && sessionId ? { session_id: sessionId } : {}) };
      const created = await base44.entities[entityName].create(payload);
      let docxUrl = null;
      if (result.type === 'report') {
        try {
          const res = await base44.functions.invoke('generateReportDocx', {
            title: result.name,
            markdown: fields.summary || '',
            sessionId,
          });
          docxUrl = res.data?.file_url || res?.file_url;
          if (docxUrl) {
            await base44.entities.Report.update(created.id, { file_url: docxUrl });
            setFileUrl(docxUrl);
          }
        } catch { /* DOCX generation optional — report is still saved */ }
      }
      setSavedId(created.id);
      setSaved(true);
      onSaved?.({ ...result, fields, id: created.id, draft: false, ...(fields.file_url ? { file_url: fields.file_url } : {}), ...(docxUrl ? { file_url: docxUrl } : {}) });
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex h-full w-full flex-col bg-card animate-slide-up">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-medium text-foreground">{result.name}</span>
          {saved && <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-primary" />}
        </div>
        <div className="flex items-center gap-2">
          {fileUrl && (
            <a href={fileUrl} target="_blank" rel="noreferrer" download={result.name?.match(/\.[a-z0-9]+$/i) ? result.name : `${result.name}.${result.fields?.file_type || 'docx'}`} className="shrink-0 text-muted-foreground transition-colors hover:text-foreground" title={t.detail.download}>
              <Download className="h-4 w-4" />
            </a>
          )}
          <button onClick={onClose} className="shrink-0 text-muted-foreground transition-colors hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        <ArtifactPreview result={{ ...result, id: saved ? savedId : result.id }} />
      </div>

      {error && <p className="px-4 pb-2 text-xs text-destructive">{error}</p>}

      {!saved ? (
        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <button onClick={handleSave} disabled={saving} className="inline-flex flex-1 items-center justify-center gap-1 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-50">
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            {saving ? t.chat.saving : t.chat.saveToSpace}
          </button>
          <button onClick={onClose} className="inline-flex items-center justify-center rounded-md border border-border px-3 py-2 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground">
            {t.chat.discard}
          </button>
        </div>
      ) : null}
    </div>
  );
}