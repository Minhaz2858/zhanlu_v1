import { Download, FileText } from 'lucide-react';

const IMG = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];
const OFFICE = ['doc', 'docx', 'ppt', 'pptx', 'pps', 'ppsx', 'xls', 'xlsx', 'odt', 'ods', 'odp'];
const AUDIO = ['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac'];
const VIDEO = ['mp4', 'mov', 'webm', 'ogv', 'mkv'];
const TEXT = ['txt', 'md', 'csv', 'json', 'log', 'tsv', 'yaml', 'yml', 'xml', 'ini', 'conf'];

export function extOf(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '');
  return m ? m[1].toLowerCase() : '';
}

export default function FilePreviewer({ url, name, kind, className }) {
  if (!url) return null;
  const ext = extOf(name || url);
  const iframeCls = `block w-full ${className || 'h-[70vh] rounded-lg border border-border bg-card'}`;

  if (IMG.includes(ext)) {
    return <img src={url} alt={name || 'file'} className={className || 'max-h-[70vh] w-full rounded-lg border border-border bg-secondary/30 object-contain'} />;
  }

  if (ext === 'pdf') {
    return <iframe src={url} title={name || 'preview'} className={iframeCls} />;
  }

  // HTML, dashboards, web apps → render in sandboxed iframe
  if (['html', 'htm'].includes(ext) || ['dashboard', 'html_file', 'webapp'].includes(kind)) {
    return <iframe src={url} title={name || 'preview'} className={iframeCls} sandbox="allow-same-origin allow-scripts allow-popups allow-forms" />;
  }

  // Audio files → native <audio> element (browser-decodable formats only).
  if (AUDIO.includes(ext)) {
    return (
      <div className="flex items-center justify-center py-12">
        <audio controls src={url} className="w-full max-w-md">
          Your browser does not support audio playback.
        </audio>
      </div>
    );
  }

  // Video files → native <video> element.
  if (VIDEO.includes(ext)) {
    return (
      <div className="flex items-center justify-center py-4">
        <video controls src={url} className="max-h-[70vh] w-full rounded-lg border border-border bg-black">
          Your browser does not support video playback.
        </video>
      </div>
    );
  }

  // Plain text / data files → iframe (browser renders natively). Falls
  // back to the download link below if the iframe is blocked by CSP.
  if (TEXT.includes(ext)) {
    return <iframe src={url} title={name || 'preview'} className={iframeCls} />;
  }

  // Office documents (docx, pptx, xlsx) → prefer self-hosted preview
  // when the URL is our own /api/artifacts/ endpoint; only reach out
  // to view.officeapps.live.com for external/legacy URLs.
  if (OFFICE.includes(ext)) {
    const isOwnApi = typeof url === 'string' && url.includes('/api/artifacts/');
    if (isOwnApi) {
      return <iframe src={url} title={name || 'preview'} className={iframeCls} />;
    }
    const viewer = `https://view.officeapps.live.com/op/embed.aspx?src=${encodeURIComponent(url)}&wdStartOn=1`;
    return <iframe src={viewer} title={name || 'preview'} className={iframeCls} allowFullScreen />;
  }

  // Fallback: download link
  return (
    <div className="flex flex-col items-center gap-3 py-12">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-secondary">
        <FileText className="h-6 w-6 text-muted-foreground" />
      </div>
      <p className="max-w-xs truncate text-sm text-foreground">{name || 'file'}</p>
      {ext && <p className="text-xs uppercase text-muted-foreground">{ext}</p>}
      <a href={url} target="_blank" rel="noreferrer" download={name} className="inline-flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground transition-colors hover:bg-secondary">
        <Download className="h-4 w-4 text-primary" /> Download
      </a>
    </div>
  );
}