import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Database, FileText, FileSpreadsheet, Loader2, UploadCloud } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { authFetch } from '@/api/authFetch';
import { appParams } from '@/lib/app-params';
import { useLanguage } from '@/lib/LanguageProvider';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import KbDatabaseFields from './KbDatabaseFields';
import KbFileFields from './KbFileFields';

const EMPTY = {
  name: '', description: '', project: 'global', source_kind: 'database',
  db_type: 'postgresql', host: '', port: '', database_name: '', username: '', password: '', api_url: '',
  file_type: '', file_url: '',
};

// A file-datasource row (spreadsheet imported as a queryable sqlite DB)
// is stored like a database KB: db_type='sqlite' + file_type=csv/xlsx/xls.
const isFileDb = (row) =>
  row && row.db_type === 'sqlite' && ['csv', 'xlsx', 'xls'].includes(String(row.file_type || '').toLowerCase());

const FILE_DB_ACCEPT = '.csv,.xlsx,.xls';

export default function KbSetupDialog({ open, onOpenChange, onSaved, editItem }) {
  const { t } = useLanguage();
  const [form, setForm] = useState(EMPTY);
  const [fileObj, setFileObj] = useState(null); // File object for file_db mode
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [projects, setProjects] = useState([]);

  useEffect(() => {
    if (open) {
      const kind = editItem ? (isFileDb(editItem) ? 'file_db' : (editItem.source_kind || 'database')) : 'database';
      setForm(editItem ? { ...EMPTY, ...editItem, source_kind: kind } : { ...EMPTY });
      setFileObj(null);
      setError('');
      base44.entities.Project.filter({ status: 'active' }).then(setProjects).catch(() => {});
    }
  }, [open, editItem]);

  const set = (k, v) => setForm((p) => ({ ...p, [k]: v }));

  function pickFile(e) {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    setFileObj(f);
    set('name', form.name.trim() || f.name.replace(/\.[^.]+$/, ''));
    set('file_type', f.name.split('.').pop().toLowerCase());
    setError('');
  }

  async function saveFileDb() {
    const body = new FormData();
    body.append('file', fileObj);
    body.append('name', form.name.trim());
    body.append('description', form.description || '');
    body.append('project', form.project || 'global');
    const res = await authFetch(
      `/api/apps/${appParams.appId || 'default-app'}/knowledge_bases/upload-query-file`,
      { method: 'POST', body },
    );
    if (!res.ok) {
      let detail = `Upload failed (${res.status})`;
      try {
        const bodyErr = await res.json();
        if (bodyErr?.detail) detail = typeof bodyErr.detail === 'string' ? bodyErr.detail : JSON.stringify(bodyErr.detail);
      } catch { /* keep status message */ }
      throw new Error(detail);
    }
    return res.json();
  }

  async function handleSubmit() {
    if (!form.name.trim()) return;
    if (form.source_kind === 'file_db') {
      if (!fileObj && !isFileDb(editItem)) {
        setError(t.kb.uploadFirst || 'Please upload a spreadsheet before saving.');
        return;
      }
      setSaving(true);
      setError('');
      try {
        let saved;
        if (fileObj) {
          saved = await saveFileDb();
        } else {
          // Edit without re-upload: only name/description/project change.
          saved = await base44.entities.KnowledgeBase.update(editItem.id, {
            name: form.name.trim(), description: form.description || '', project: form.project || 'global',
          });
        }
        onSaved?.(saved);
        onOpenChange(false);
      } catch (e) {
        setError(e.message || 'Unable to save spreadsheet data source');
      } finally {
        setSaving(false);
      }
      return;
    }
    if (form.source_kind === 'file' && !form.file_url) {
      setError(t.kb.uploadFirst || 'Please upload a file before saving.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const payload = { ...form, status: form.status || 'active' };
      if (form.port === '' || form.port === null || form.port === undefined) delete payload.port;
      else payload.port = Number(form.port);
      if (form.source_kind === 'file') {
        ['db_type', 'host', 'port', 'database_name', 'username', 'password', 'api_url'].forEach((key) => delete payload[key]);
      } else {
        ['file_type', 'file_url'].forEach((key) => delete payload[key]);
        if (form.db_type === 'api') ['host', 'port', 'database_name', 'username', 'password'].forEach((key) => delete payload[key]);
        else delete payload.api_url;
      }
      let saved = editItem
        ? await base44.entities.KnowledgeBase.update(editItem.id, payload)
        : await base44.entities.KnowledgeBase.create(payload);
      // Run document indexing for file KBs, then re-fetch so the card
      // gets the updated indexing_status / chunk_count (the `saved` object
      // from update/create reflects the pre-reindex state).
      if (saved?.source_kind === 'file' && saved?.file_url) {
        try {
          await authFetch(`/api/apps/${saved.app_id || 'default-app'}/knowledge_bases/${saved.id}/reindex`, { method: 'POST' });
          saved = await base44.entities.KnowledgeBase.get(saved.id);
        } catch { /* non-fatal — user can reindex from the card */ }
      }
      // Trigger catalog discovery for database KBs (belt-and-suspenders —
      // the backend entities hook also fires; the /catalog/reindex endpoint
      // guards against duplicate indexing).
      if (saved?.source_kind === 'database' && ['mysql','postgres','postgresql'].includes((saved?.db_type||'').toLowerCase())) {
        try {
          await authFetch(`/api/apps/${saved.app_id || 'default-app'}/knowledge_bases/${saved.id}/catalog/reindex`, { method: 'POST' });
        } catch { /* non-fatal — backend auto-trigger also covers this */ }
      }
      onSaved?.(saved);
      onOpenChange(false);
    } catch (e) {
      setError(e.message || 'Unable to save data source');
    } finally {
      setSaving(false);
    }
  }

  const hasFileDbFile = !!fileObj || isFileDb(editItem);
  const canSubmit = form.name.trim().length > 0
    && (form.source_kind === 'database' ? !!form.db_type : form.source_kind === 'file_db' ? hasFileDbFile : !!form.file_url);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{editItem ? t.kb.editKb : t.kb.newKb}</DialogTitle>
          <DialogDescription>{t.kb.desc}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <Label className="mb-1.5 block text-xs">{t.kb.name}</Label>
            <Input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder={t.kb.namePh} />
          </div>
          <div>
            <Label className="mb-1.5 block text-xs">{t.createDialog.project}</Label>
            <Select value={form.project || 'global'} onValueChange={(v) => set('project', v)}>
              <SelectTrigger><SelectValue placeholder={t.createDialog.projectPh} /></SelectTrigger>
              <SelectContent>
                <SelectItem value="global">{t.createDialog.globalProject}</SelectItem>
                {projects.filter((p) => p.name !== 'Ungrouped' && p.name !== '未分组').map((p) => (
                  <SelectItem key={p.id} value={p.name}>{p.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label className="mb-1.5 block text-xs">{t.kb.sourceKind}</Label>
            <div className="grid grid-cols-2 gap-2">
              {['database', 'file'].map((k) => (
                <button key={k} type="button" onClick={() => set('source_kind', k)} className={`flex items-center gap-2 rounded-lg border px-3 py-2.5 text-left text-xs transition-colors ${form.source_kind === k ? 'border-primary bg-primary/5 text-foreground' : 'border-border text-muted-foreground hover:text-foreground'}`}>
                  {k === 'database' ? <Database className="h-4 w-4" /> : <FileText className="h-4 w-4" />}
                  {t.kb.sourceKinds[k]}
                </button>
              ))}
              <button type="button" onClick={() => set('source_kind', 'file_db')} className={`col-span-2 flex items-center gap-2 rounded-lg border px-3 py-2.5 text-left text-xs transition-colors ${form.source_kind === 'file_db' ? 'border-primary bg-primary/5 text-foreground' : 'border-border text-muted-foreground hover:text-foreground'}`}>
                <FileSpreadsheet className="h-4 w-4" />
                {t.kb.sourceKinds.file_db}
                <span className="ml-auto text-[10px] opacity-70">{t.kb.fileDbHint || 'Query it in Database Query like a connected database'}</span>
              </button>
            </div>
          </div>
          {form.source_kind === 'database'
            ? <KbDatabaseFields value={form} onChange={(patch) => setForm((p) => ({ ...p, ...patch }))} t={t} />
            : form.source_kind === 'file_db'
              ? (
                <div>
                  <Label className="mb-1.5 block text-xs">{t.kb.uploadFile}</Label>
                  <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-dashed px-3 py-2.5 text-xs text-muted-foreground hover:text-foreground">
                    <UploadCloud className="h-4 w-4" />
                    <span className="flex-1 truncate">
                      {fileObj ? fileObj.name : isFileDb(editItem) ? (editItem.name || editItem.file_url) : (t.kb.uploadHint || 'Upload a .csv / .xlsx / .xls spreadsheet')}
                    </span>
                    <Button type="button" variant="outline" size="sm" onClick={(e) => { e.preventDefault(); e.stopPropagation(); const el = document.getElementById('kb-file-db-input'); if (el) el.click(); }}>
                      {t.kb.browse || 'Browse'}
                    </Button>
                    <input id="kb-file-db-input" type="file" accept={FILE_DB_ACCEPT} className="hidden" onChange={pickFile} disabled={saving} />
                  </label>
                  <p className="mt-1 text-[10px] text-muted-foreground">{t.kb.fileDbExplain || 'Each sheet becomes a table. Ask questions in plain language or write SQL.'}</p>
                </div>
              )
              : <KbFileFields value={form} onChange={(patch) => setForm((p) => ({ ...p, ...patch }))} t={t} />}
          <div>
            <Label className="mb-1.5 block text-xs">{t.kb.description}</Label>
            <Textarea value={form.description} onChange={(e) => set('description', e.target.value)} placeholder={t.kb.descPh} rows={2} className="resize-none" />
          </div>
        </div>

        {error && <p className="text-xs text-destructive" role="alert">{error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t.kb.cancel}</Button>
          <Button onClick={handleSubmit} disabled={!canSubmit || saving}>
            {saving && <Loader2 className="h-4 w-4 animate-spin" />} {saving ? t.kb.saving : t.kb.save}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
