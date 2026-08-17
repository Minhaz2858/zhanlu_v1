import { useState } from 'react';
import { Label } from '@/components/ui/label';
import { UploadCloud, File as FileIcon, Loader2 } from 'lucide-react';
import { base44 } from '@/api/base44Client';
import { toast } from 'sonner';

function extOf(name) {
  const m = (name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
  if (!m) return '';
  return m[1] === 'xlsx' || m[1] === 'xls' ? 'excel' : m[1];
}

export default function KbFileFields({ value, onChange, t }) {
  const [uploading, setUploading] = useState(false);

  async function handleFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const { file_url } = await base44.integrations.Core.UploadFile({ file });
      onChange({ ...value, file_url, file_type: extOf(file.name), name: value.name || file.name.replace(/\.[^.]+$/, '') });
    } catch (err) {
      console.error('[KbFileFields] upload failed:', err);
      toast.error(err?.message || t.kb.uploadFailed || 'Upload failed');
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="space-y-2">
      <Label className="block text-xs">{t.kb.uploadFile}</Label>
      <div className="flex items-center gap-2 rounded-lg border border-border bg-secondary/40 px-3 py-2.5">
        <FileIcon className="h-4 w-4 shrink-0 text-primary" />
        <span className="flex-1 truncate text-xs text-foreground">{value.file_url ? (value.name || 'file') : (uploading ? t.kb.uploading : t.kb.uploadHint)}</span>
        {value.file_type && <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground">{value.file_type}</span>}
        <label className="inline-flex cursor-pointer items-center gap-1 text-xs text-primary hover:underline">
          {uploading ? <Loader2 className="h-3 w-3 animate-spin" /> : <UploadCloud className="h-3 w-3" />}
          <input type="file" accept=".pdf,.docx,.md,.csv,.xlsx,.xls,.json,.txt" className="hidden" onChange={handleFile} disabled={uploading} />
        </label>
      </div>
    </div>
  );
}