import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Database, FileText, Loader2 } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { authFetch } from '@/api/authFetch';
import { useLanguage } from '@/lib/LanguageProvider';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import KbDatabaseFields from './KbDatabaseFields';
import KbFileFields from './KbFileFields';

const EMPTY = {
  name: '', description: '', project: 'global', source_kind: 'database',
  db_type: 'postgresql', host: '', port: '', database_name: '', username: '', password: '', api_url: '',
  file_type: '', file_url: '',
};

export default function KbSetupDialog({ open, onOpenChange, onSaved, editItem }) {
  const { t } = useLanguage();
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [projects, setProjects] = useState([]);

  useEffect(() => {
    if (open) {
      setForm(editItem ? { ...EMPTY, ...editItem } : { ...EMPTY });
      setError('');
      base44.entities.Project.filter({ status: 'active' }).then(setProjects).catch(() => {});
    }
  }, [open, editItem]);

  const set = (k, v) => setForm((p) => ({ ...p, [k]: v }));

  async function handleSubmit() {
    if (!form.name.trim()) return;
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
    } finally { setSaving(false); }
  }

  const canSubmit = form.name.trim().length > 0 && (form.source_kind === 'file' ? !!form.file_url : !!form.db_type);

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
            </div>
          </div>
          {form.source_kind === 'database'
            ? <KbDatabaseFields value={form} onChange={(patch) => setForm((p) => ({ ...p, ...patch }))} t={t} />
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