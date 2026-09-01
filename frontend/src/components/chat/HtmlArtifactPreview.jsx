import { useEffect, useState } from 'react';
import { Code2, Copy, ExternalLink, Eye, Loader2 } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

export default function HtmlArtifactPreview({ url, content = '' }) {
  const { lang } = useLanguage();
  const [mode, setMode] = useState('preview');
  const [html, setHtml] = useState(content);
  const [loading, setLoading] = useState(!content && !!url);

  useEffect(() => {
    if (content || !url) return;
    let active = true;
    fetch(url).then((res) => res.text()).then((text) => { if (active) setHtml(text); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [url, content]);

  if (loading) return <div className="flex items-center gap-2 p-4 text-xs text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />{lang === 'en' ? 'Loading HTML…' : '正在加载 HTML…'}</div>;
  return (
    <div className="flex h-full min-h-[560px] flex-col overflow-hidden rounded-lg border border-border bg-background">
      <div className="flex items-center gap-1 border-b border-border p-2">
        <button onClick={() => setMode('preview')} className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs ${mode === 'preview' ? 'bg-secondary text-foreground' : 'text-muted-foreground'}`}><Eye className="h-3.5 w-3.5" />{lang === 'en' ? 'Preview' : '预览'}</button>
        <button onClick={() => setMode('code')} className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs ${mode === 'code' ? 'bg-secondary text-foreground' : 'text-muted-foreground'}`}><Code2 className="h-3.5 w-3.5" />{lang === 'en' ? 'Code' : '代码'}</button>
        <div className="ml-auto flex items-center gap-1">
          {mode === 'code' && <button onClick={() => navigator.clipboard.writeText(html)} className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground" title="Copy"><Copy className="h-3.5 w-3.5" /></button>}
          {url && <a href={url} target="_blank" rel="noreferrer" className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary hover:text-foreground"><ExternalLink className="h-3.5 w-3.5" /></a>}
        </div>
      </div>
      {mode === 'preview' ? <iframe srcDoc={html} title="HTML preview" className="min-h-0 flex-1 bg-card" sandbox="allow-scripts allow-forms allow-modals allow-popups" /> : <pre className="min-h-0 flex-1 overflow-auto p-4 font-mono text-xs leading-relaxed text-foreground"><code>{html}</code></pre>}
    </div>
  );
}