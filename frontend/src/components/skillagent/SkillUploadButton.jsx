import { useRef, useState, useEffect } from 'react';
import { Paperclip, Loader2, File, Folder, Archive } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';

export default function SkillUploadButton({ onUploaded, disabled, variant = 'compact', ctaLabel, ctaHint }) {
  const { t } = useLanguage();
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef(null);
  const folderInputRef = useRef(null);
  const zipInputRef = useRef(null);

  useEffect(() => {
    if (folderInputRef.current) {
      folderInputRef.current.setAttribute('webkitdirectory', '');
      folderInputRef.current.setAttribute('directory', '');
    }
  }, [folderInputRef]);

  async function uploadFiles(files, isFolder) {
    const arr = Array.from(files || []);
    if (arr.length === 0) return;
    const relevant = arr.filter((f) => !f.name.startsWith('.') && f.name !== '.DS_Store' && f.size > 0);
    setUploading(true);
    try {
      const uploaded = [];
      for (const file of relevant.slice(0, 50)) {
        const { file_url } = await base44.integrations.Core.UploadFile({ file });
        uploaded.push({ name: isFolder ? (file.webkitRelativePath || file.name) : file.name, url: file_url });
      }
      onUploaded?.(uploaded);
    } finally {
      setUploading(false);
    }
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          {variant === 'cta' ? (
            <button
              type="button"
              disabled={disabled || uploading}
              className="flex w-full flex-col items-center gap-1.5 rounded-xl border border-dashed border-primary/40 bg-primary/5 px-4 py-6 text-center transition hover:border-primary hover:bg-primary/10 disabled:opacity-50"
            >
              {uploading ? (
                <Loader2 className="h-5 w-5 animate-spin text-primary" />
              ) : (
                <Paperclip className="h-5 w-5 text-primary" />
              )}
              <span className="text-sm font-medium text-foreground">{ctaLabel}</span>
              {ctaHint && <span className="text-[11px] text-muted-foreground">{ctaHint}</span>}
            </button>
          ) : (
            <Button variant="ghost" size="icon" disabled={disabled || uploading} className="h-8 w-8 shrink-0">
              {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
            </Button>
          )}
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem onClick={() => fileInputRef.current?.click()}>
            <File className="h-4 w-4" /> {t.skillAgent.uploadFiles}
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => folderInputRef.current?.click()}>
            <Folder className="h-4 w-4" /> {t.skillAgent.uploadFolder}
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => zipInputRef.current?.click()}>
            <Archive className="h-4 w-4" /> {t.skillAgent.uploadZip}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <input ref={fileInputRef} type="file" accept=".json,.md,.yaml,.yml,.txt,.py,.js,.ts" multiple className="hidden" onChange={(e) => { uploadFiles(e.target.files, false); e.target.value = ''; }} />
      <input ref={folderInputRef} type="file" className="hidden" onChange={(e) => { uploadFiles(e.target.files, true); e.target.value = ''; }} />
      <input ref={zipInputRef} type="file" accept=".zip" className="hidden" onChange={(e) => { uploadFiles(e.target.files, false); e.target.value = ''; }} />
    </>
  );
}