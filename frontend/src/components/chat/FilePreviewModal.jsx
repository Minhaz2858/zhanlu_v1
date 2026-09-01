import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { Download } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import FilePreviewer from './FilePreviewer';

export default function FilePreviewModal({ file, open, onOpenChange }) {
  const { t } = useLanguage();
  if (!file) return null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[85vh] max-w-4xl flex-col p-0">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3 pr-12">
          <DialogTitle className="truncate text-sm font-medium text-foreground">{file.name}</DialogTitle>
          {file.file_url && (
            <a href={file.download_url || file.file_url} target="_blank" rel="noreferrer" download={file.name} className="inline-flex shrink-0 items-center gap-1.5 rounded-xl bg-[#F2F0ED] px-3 py-1.5 text-xs font-medium text-[#1A1A1A] transition-opacity hover:opacity-80">
              <Download className="h-3.5 w-3.5 text-[#B87333]" /> {t.detail.download}{file.file_type ? ` (.${file.file_type})` : ''}
            </a>
          )}
        </div>
        <div className="flex-1 overflow-auto p-4">
          <FilePreviewer url={file.file_url} name={file.name} kind={file.resource_kind} fileType={file.file_type} />
        </div>
      </DialogContent>
    </Dialog>
  );
}