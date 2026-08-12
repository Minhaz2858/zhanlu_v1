import { useState, useEffect } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import { formatDate } from '@/lib/time';
import { authFetch } from '@/api/authFetch';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { FileText, Image as ImageIcon, Code2, Download, Loader2, Eye } from 'lucide-react';
import FilePreviewModal from './FilePreviewModal';

const TABS = ['all', 'docs', 'images', 'code', 'media'];

function fileCategory(file) {
  const ext = (file.file_type || '').toLowerCase().replace(/^\./, '');
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp', 'tiff', 'tif'].includes(ext)) return 'images';
  if (['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac', 'mp4', 'mov', 'webm', 'mkv'].includes(ext)) return 'media';
  if (['tsx', 'ts', 'js', 'jsx', 'py', 'json', 'html', 'htm', 'css', 'go', 'rs', 'java', 'c', 'cpp', 'sh', 'yml', 'yaml', 'xml', 'ini', 'conf'].includes(ext)) return 'code';
  return 'docs';
}

function FileIcon({ file }) {
  const cat = fileCategory(file);
  if (cat === 'images') return <ImageIcon className="h-4 w-4 text-primary" />;
  if (cat === 'code') return <Code2 className="h-4 w-4 text-primary" />;
  return <FileText className="h-4 w-4 text-primary" />;
}


export default function ChatFilesModal({ sessionId, open, onOpenChange }) {
  const { t } = useLanguage();
  const [files, setFiles] = useState([]);
  const [task, setTask] = useState(null); // { task_id, task_name } | null
  const [tab, setTab] = useState('all');
  const [loading, setLoading] = useState(false);
  const [previewFile, setPreviewFile] = useState(null);

  useEffect(() => {
    if (!open || !sessionId) return;
    setLoading(true);
    // Manus-style: surface AI-generated AutomationFile rows from executions of
    // the session's task (primary), with UserFile uploads as a secondary list.
    // Backend route: GET /api/automations/by-session/{session_id}/files
    authFetch(`/api/automations/by-session/${sessionId}/files`)
      .then((r) => (r.ok ? r.json() : { task_id: null, task_name: null, files: [] }))
      .then((data) => {
        setTask(
          data?.task_id ? { task_id: data.task_id, task_name: data.task_name } : null
        );
        setFiles(Array.isArray(data?.files) ? data.files : []);
      })
      .catch(() => {
        setTask(null);
        setFiles([]);
      })
      .finally(() => setLoading(false));
  }, [open, sessionId]);

  const filtered = tab === 'all' ? files : files.filter((f) => fileCategory(f) === tab);

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {t.chat.chatFiles.title}
              {task?.task_name ? (
                <span className="ml-2 text-sm font-normal text-gray-500">
                  — {task.task_name}
                </span>
              ) : null}
            </DialogTitle>
          </DialogHeader>
          <div className="flex flex-wrap gap-1.5">
            {TABS.map((tb) => (
              <button
                key={tb}
                onClick={() => setTab(tb)}
                className={`rounded-full px-3 py-1 text-xs transition-colors ${tab === tb ? 'bg-secondary text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
              >
                {t.chat.chatFiles[tb]}
              </button>
            ))}
          </div>
          <div className="max-h-80 overflow-y-auto">
            {loading ? (
              <div className="flex justify-center py-8"><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></div>
            ) : filtered.length === 0 ? (
              <p className="py-8 text-center text-xs text-muted-foreground">{t.chat.chatFiles.empty}</p>
            ) : (
              filtered.map((f) => (
                <div key={f.id} className="flex cursor-pointer items-center gap-3 border-b border-border py-2.5 last:border-0 hover:bg-secondary/40" onClick={() => f.file_url && setPreviewFile(f)}>
                  <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-secondary">
                    <FileIcon file={f} />
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm text-foreground">{f.name}</p>
                    <p className="text-[11px] text-muted-foreground">{formatDate(f.updated_date)}</p>
                  </div>
                  {f.file_url && (
                    <div className="flex items-center gap-2">
                      <button onClick={(e) => { e.stopPropagation(); setPreviewFile(f); }} className="text-muted-foreground transition-colors hover:text-primary" title={t.detail.preview}>
                        <Eye className="h-4 w-4" />
                      </button>
                      <a href={f.download_url || f.file_url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className="text-muted-foreground transition-colors hover:text-primary">
                        <Download className="h-4 w-4" />
                      </a>
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </DialogContent>
      </Dialog>
      <FilePreviewModal file={previewFile} open={!!previewFile} onOpenChange={(o) => { if (!o) setPreviewFile(null); }} />
    </>
  );
}