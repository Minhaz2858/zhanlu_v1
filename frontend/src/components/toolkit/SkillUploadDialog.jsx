import { useState, useEffect, useRef } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Globe, Github, UploadCloud, File as FileIcon, Loader2, Download, CheckCircle2, AlertCircle, Folder, Archive } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';

export default function SkillUploadDialog({ open, onOpenChange, onSaved, kind, initialMode }) {
  const { t } = useLanguage();
  const [mode, setMode] = useState('url');
  const [uploading, setUploading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [collectUrl, setCollectUrl] = useState('');
  const [collecting, setCollecting] = useState(false);
  const [collectResult, setCollectResult] = useState(null);
  const [uploadMode, setUploadMode] = useState('single');
  const [singleFile, setSingleFile] = useState(null);
  const [uploadedFiles, setUploadedFiles] = useState([]);
  const folderInputRef = useRef(null);

  // Detect if the URL is a GitHub URL
  function isGithubUrl(url) {
    return url && (url.includes('github.com') || url.includes('githubusercontent.com'));
  }

  useEffect(() => {
    if (folderInputRef.current) {
      folderInputRef.current.setAttribute('webkitdirectory', '');
      folderInputRef.current.setAttribute('directory', '');
    }
  }, [folderInputRef, mode, uploadMode]);

  useEffect(() => {
    if (open) {
      setCollectResult(null);
      setCollectUrl('');
      setUploadMode('single');
      setSingleFile(null);
      setUploadedFiles([]);
      setMode(initialMode === 'github' ? 'url' : (initialMode || 'url'));
    }
  }, [open, initialMode]);

  async function handleFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const { file_url } = await base44.integrations.Core.UploadFile({ file });
      setSingleFile({ name: file.name, url: file_url });
    } catch { /* ignore */ }
    finally { setUploading(false); }
  }

  async function handleFolderUpload(e) {
    const files = Array.from(e.target.files || []);
    if (files.length === 0) return;
    const relevant = files.filter((f) => !f.name.startsWith('.') && f.name !== '.DS_Store' && f.size > 0);
    setUploading(true);
    try {
      const uploaded = [];
      for (const file of relevant.slice(0, 50)) {
        const { file_url } = await base44.integrations.Core.UploadFile({ file });
        uploaded.push({ name: file.webkitRelativePath || file.name, url: file_url });
      }
      setUploadedFiles(uploaded);
    } catch { /* ignore */ }
    finally { setUploading(false); e.target.value = ''; }
  }

  async function handleZipUpload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const { file_url } = await base44.integrations.Core.UploadFile({ file });
      setUploadedFiles([{ name: file.name, url: file_url }]);
    } catch { /* ignore */ }
    finally { setUploading(false); e.target.value = ''; }
  }

  async function handleCollect() {
    if (!collectUrl.trim()) return;
    setCollecting(true);
    setCollectResult(null);
    try {
      const res = await base44.functions.invoke('collectSkills', { url: collectUrl.trim(), kind: kind || 'system_skill' });
      const data = res.data || res;
      setCollectResult(data);
      if (data.success && data.collected > 0) onSaved?.();
    } catch (e) {
      setCollectResult({ success: false, error: e.message || t.toolkit.collectError });
    } finally {
      setCollecting(false);
    }
  }

  function deriveName() {
    if (uploadMode === 'single' && singleFile) return singleFile.name.replace(/\.[^.]+$/, '');
    if (uploadMode === 'folder' && uploadedFiles.length) return (uploadedFiles[0].name.split('/')[0] || 'skill-folder');
    if (uploadMode === 'zip' && uploadedFiles.length) return uploadedFiles[0].name.replace(/\.[^.]+$/, '');
    return '';
  }

  async function handleSave() {
    const name = deriveName();
    if (!name) return;
    setSaving(true);
    try {
      let skill_file_url = '';
      let sources = [];
      if (uploadMode === 'single' && singleFile) {
        skill_file_url = singleFile.url;
      } else if (uploadMode === 'folder' && uploadedFiles.length) {
        sources = uploadedFiles.map((f) => f.url);
        const skillMd = uploadedFiles.find((f) => f.name.endsWith('SKILL.md'));
        if (skillMd) skill_file_url = skillMd.url;
      } else if (uploadMode === 'zip' && uploadedFiles.length) {
        skill_file_url = uploadedFiles[0].url;
      }
      await base44.entities.Tool.create({
        name, kind: kind || 'system_skill', source: 'file',
        skill_file_url, sources, status: 'active',
      });
      onSaved?.();
      onOpenChange(false);
    } finally { setSaving(false); }
  }

  const canSave = deriveName().length > 0;
  const uploadTabs = [
    { key: 'single', label: t.toolkit.uploadModeFile, icon: FileIcon },
    { key: 'folder', label: t.toolkit.uploadModeFolder, icon: Folder },
    { key: 'zip', label: t.toolkit.uploadModeZip, icon: Archive },
  ];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t.toolkit.addSkill}</DialogTitle>
          <DialogDescription>{mode === 'url' ? t.toolkit.collectDescUrl : t.toolkit.uploadDesc}</DialogDescription>
        </DialogHeader>

        <div>
          <Label className="mb-1.5 block text-xs">{t.toolkit.sourceLabel}</Label>
          <div className="grid grid-cols-2 gap-2">
            <button type="button" onClick={() => { setMode('url'); setCollectResult(null); }} className={`flex items-center gap-2 rounded-lg border px-3 py-2.5 text-left text-xs transition-colors ${mode === 'url' ? 'border-primary bg-primary/5 text-foreground' : 'border-border text-muted-foreground hover:text-foreground'}`}>
              <Globe className="h-4 w-4" /> {t.toolkit.sourceUrl}
            </button>
            <button type="button" onClick={() => setMode('file')} className={`flex items-center gap-2 rounded-lg border px-3 py-2.5 text-left text-xs transition-colors ${mode === 'file' ? 'border-primary bg-primary/5 text-foreground' : 'border-border text-muted-foreground hover:text-foreground'}`}>
              <UploadCloud className="h-4 w-4" /> {t.toolkit.sourceFile}
            </button>
          </div>
        </div>

        {mode === 'url' ? (
          <div className="space-y-3">
            <div>
              <Label className="mb-1.5 block text-xs">{t.toolkit.urlLabel}</Label>
              <div className="relative">
                <input
                  value={collectUrl}
                  onChange={(e) => setCollectUrl(e.target.value)}
                  placeholder={t.toolkit.urlPlaceholder}
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
                {collectUrl.trim() && isGithubUrl(collectUrl.trim()) && (
                  <span className="absolute right-2.5 top-1/2 -translate-y-1/2">
                    <Github className="h-4 w-4 text-muted-foreground" />
                  </span>
                )}
              </div>
              <p className="mt-1.5 text-[11px] text-muted-foreground">{t.toolkit.urlHint}</p>
            </div>
            <Button onClick={handleCollect} disabled={collecting || !collectUrl.trim()} className="w-full">
              {collecting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />} {collecting ? t.toolkit.collecting : t.toolkit.collect}
            </Button>

            {collectResult && (
              <div className="space-y-2">
                {collectResult.error ? (
                  <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive">
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>{collectResult.error}</span>
                  </div>
                ) : collectResult.success ? (
                  <>
                    <div className="rounded-lg border border-green-200 bg-green-50 p-3 text-xs">
                      <div className="mb-1.5 flex items-center gap-1.5 font-medium text-green-700">
                        <CheckCircle2 className="h-4 w-4" /> {t.toolkit.collectedN.replace('{n}', collectResult.collected)}
                      </div>
                      {collectResult.skills && collectResult.skills.length > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {collectResult.skills.map((s) => (
                            <span key={s.id} className="rounded bg-green-100 px-1.5 py-0.5 font-mono text-[10px] text-green-700">{s.name}</span>
                          ))}
                        </div>
                      )}
                      {collectResult.errors && collectResult.errors.length > 0 && (
                        <p className="mt-1.5 text-[11px] text-amber-600">{collectResult.errors.length} skill(s) skipped</p>
                      )}
                      {collectResult.info && collectResult.collected === 0 && (
                        <p className="mt-1 text-[11px] text-muted-foreground">{collectResult.info}</p>
                      )}
                    </div>
                    {collectResult.collected > 0 && (
                      <Button variant="outline" size="sm" className="w-full" onClick={() => onOpenChange(false)}>{t.toolkit.done}</Button>
                    )}
                  </>
                ) : null}
              </div>
            )}
          </div>
        ) : (
          <>
            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <Label className="text-xs">{t.toolkit.skillFile}</Label>
                <div className="flex gap-1">
                  {uploadTabs.map((opt) => (
                    <button key={opt.key} type="button" onClick={() => { setUploadMode(opt.key); setSingleFile(null); setUploadedFiles([]); }} className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] transition-colors ${uploadMode === opt.key ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'}`}>
                      <opt.icon className="h-3 w-3" /> {opt.label}
                    </button>
                  ))}
                </div>
              </div>

              {uploadMode === 'single' && (
                <div className="flex items-center gap-2 rounded-lg border border-border bg-secondary/40 px-3 py-2.5">
                  <FileIcon className="h-4 w-4 shrink-0 text-primary" />
                  <span className="flex-1 truncate text-xs text-foreground">{singleFile ? singleFile.name : (uploading ? t.toolkit.uploading : t.toolkit.uploadHint)}</span>
                  <label className="inline-flex cursor-pointer items-center gap-1 text-xs text-primary hover:underline">
                    {uploading ? <Loader2 className="h-3 w-3 animate-spin" /> : <UploadCloud className="h-3 w-3" />}
                    <input type="file" accept=".json,.md,.yaml,.yml,.txt" className="hidden" onChange={handleFile} disabled={uploading} />
                  </label>
                </div>
              )}

              {uploadMode === 'folder' && (
                <div className="flex items-center gap-2 rounded-lg border border-border bg-secondary/40 px-3 py-2.5">
                  <Folder className="h-4 w-4 shrink-0 text-primary" />
                  <span className="flex-1 truncate text-xs text-foreground">{uploadedFiles.length > 0 ? t.toolkit.uploadedFiles.replace('{n}', uploadedFiles.length) : (uploading ? t.toolkit.uploading : t.toolkit.folderHint)}</span>
                  <label className="inline-flex cursor-pointer items-center gap-1 text-xs text-primary hover:underline">
                    {uploading ? <Loader2 className="h-3 w-3 animate-spin" /> : <UploadCloud className="h-3 w-3" />}
                    <input ref={folderInputRef} type="file" className="hidden" onChange={handleFolderUpload} disabled={uploading} />
                  </label>
                </div>
              )}

              {uploadMode === 'zip' && (
                <div className="flex items-center gap-2 rounded-lg border border-border bg-secondary/40 px-3 py-2.5">
                  <Archive className="h-4 w-4 shrink-0 text-primary" />
                  <span className="flex-1 truncate text-xs text-foreground">{uploadedFiles.length > 0 ? uploadedFiles[0].name : (uploading ? t.toolkit.uploading : t.toolkit.zipHint)}</span>
                  <label className="inline-flex cursor-pointer items-center gap-1 text-xs text-primary hover:underline">
                    {uploading ? <Loader2 className="h-3 w-3 animate-spin" /> : <UploadCloud className="h-3 w-3" />}
                    <input type="file" accept=".zip" className="hidden" onChange={handleZipUpload} disabled={uploading} />
                  </label>
                </div>
              )}

              {(uploadMode === 'folder' || uploadMode === 'zip') && uploadedFiles.length > 0 && (
                <div className="mt-2 max-h-32 overflow-y-auto rounded-lg border border-border bg-secondary/20 p-2">
                  {uploadedFiles.map((f, i) => (
                    <div key={i} className="flex items-center gap-2 px-1 py-0.5 text-xs text-muted-foreground">
                      <FileIcon className="h-3 w-3 shrink-0 text-primary" />
                      <span className="truncate">{f.name}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>{t.toolkit.cancel}</Button>
              <Button onClick={handleSave} disabled={!canSave || saving || uploading}>
                {saving && <Loader2 className="h-4 w-4 animate-spin" />} {saving ? t.toolkit.saving : t.toolkit.save}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}