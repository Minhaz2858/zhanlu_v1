import { useEffect, useRef, useState } from 'react';
import { X, Loader2, Save, Eye, Code2, AlertCircle, CheckCircle2, RotateCcw } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * ArtifactCanvasEditor — interactive canvas for HTML artifacts.
 *
 * End-to-end edit loop backed by the artifact canvas API:
 *   - GET  /api/artifacts/{id}/versions → latest source_json.html is the
 *     editable source (fallback: GET /api/artifacts/{id}/preview raw text)
 *   - POST /api/artifacts/{id}/canvas/save → persists a NEW immutable
 *     version ({ html, changelog, source: "user" }) so the chat preview
 *     and download both reflect the edit without an LLM round-trip.
 *
 * Layout: split view — code editor (left) and sandboxed live preview
 * (right, sandbox="allow-scripts" iframe). Save button creates the new
 * version; the version list under the editor shows the immutable trail.
 */
function artifactIdOf(a) {
  return a?.artifact_id || a?.id || a?.artifactId;
}

export default function ArtifactCanvasEditor({ artifact, onClose, onSaved }) {
  const { lang } = useLanguage();
  const isEn = lang === 'en';
  const T = (zh, en) => (isEn ? en : zh);

  const id = artifactIdOf(artifact);
  const [html, setHtml] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [versions, setVersions] = useState([]);
  const [mode, setMode] = useState('edit'); // edit | preview
  const [lastError, setLastError] = useState(null);
  const textareaRef = useRef(null);

  // Load the current HTML source on mount.
  useEffect(() => {
    if (!id) { setLoading(false); setError(T('缺少 artifact id', 'Missing artifact id')); return; }
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        let src = null;
        let verRows = [];
        // 1) Try the versions trail — source_json.html carries the editable
        //    document for html/html_report artifacts.
        try {
          const res = await authFetch(`/api/artifacts/${id}/versions`);
          if (res.ok) {
            const data = await res.json();
            if (Array.isArray(data)) verRows = data;
            const withHtml = [...verRows].reverse().find((v) => v?.source_json?.html);
            if (withHtml?.source_json?.html) src = withHtml.source_json.html;
          }
        } catch { /* fall through */ }
        // 2) Fallback: fetch the preview blob as raw text.
        if (src == null) {
          const res = await authFetch(`/api/artifacts/${id}/preview`);
          if (res.ok) src = await res.text();
        }
        if (!cancelled) {
          if (src == null) setError(T('无法读取 HTML 源码', 'Could not read HTML source'));
          else setHtml(src);
          setVersions(verRows);
        }
      } catch (e) {
        if (!cancelled) setError(e.message || String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleSave() {
    if (!id || !html.trim()) return;
    setSaving(true);
    setLastError(null);
    setSaved(false);
    try {
      const res = await authFetch(`/api/artifacts/${id}/canvas/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          html,
          changelog: T('在画布中编辑', 'Edited in canvas'),
          source: 'user',
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      setSaved(true);
      onSaved?.(data);
      // Refresh version trail so the new version shows up.
      try {
        const res2 = await authFetch(`/api/artifacts/${id}/versions`);
        if (res2.ok) setVersions(await res2.json());
      } catch { /* non-fatal */ }
    } catch (e) {
      setLastError(e.message || String(e));
    } finally {
      setSaving(false);
    }
  }

  const isHtml = (artifact?.type || artifact?.artifact_type || '').toLowerCase() === 'html'
    || (artifact?.type || artifact?.artifact_type || '').toLowerCase() === 'html_report';

  return (
    <div className="flex h-full flex-col bg-background">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Code2 className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-foreground">
              {T('交互式画布', 'Interactive Canvas')}
            </h2>
            <p className="truncate text-[10px] text-muted-foreground">
              {artifact?.title || artifact?.file_name || ''}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {/* Edit / preview toggle */}
          <div className="mr-1 flex items-center rounded-lg border border-border bg-secondary/40 p-0.5">
            <button
              type="button"
              onClick={() => setMode('edit')}
              className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${mode === 'edit' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
            >
              <Code2 className="h-3 w-3" /> {T('代码', 'Code')}
            </button>
            <button
              type="button"
              onClick={() => setMode('preview')}
              className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${mode === 'preview' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
            >
              <Eye className="h-3 w-3" /> {T('预览', 'Preview')}
            </button>
          </div>
          <button
            type="button"
            onClick={handleSave}
            disabled={saving || loading || !html.trim()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            {saving ? T('保存中…', 'Saving…') : T('保存新版本', 'Save new version')}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            aria-label={T('关闭', 'Close')}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1">
        {loading ? (
          <div className="flex h-full items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : error ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
            <AlertCircle className="h-6 w-6 text-red-500" />
            <p className="text-xs text-muted-foreground">{error}</p>
          </div>
        ) : (
          <div className="grid h-full grid-cols-1 md:grid-cols-2">
            {/* Editor */}
            <div className={`min-h-0 flex flex-col border-border ${mode === 'preview' ? 'hidden md:flex' : 'flex'}`}>
              <div className="flex items-center justify-between border-b border-border/60 px-3 py-1.5">
                <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {T('HTML 源码', 'HTML source')}
                </span>
                <button
                  type="button"
                  onClick={() => textareaRef.current?.focus()}
                  className="text-[10px] text-muted-foreground hover:text-foreground"
                >
                  {html.length} {T('字符', 'chars')}
                </button>
              </div>
              <textarea
                ref={textareaRef}
                value={html}
                onChange={(e) => { setHtml(e.target.value); setSaved(false); }}
                spellCheck={false}
                className="min-h-0 flex-1 resize-none bg-background p-3 font-mono text-[11px] leading-relaxed text-foreground outline-none"
                placeholder="<!DOCTYPE html>…"
              />
            </div>
            {/* Preview */}
            <div className={`min-h-0 flex flex-col border-l border-border ${mode === 'edit' ? 'hidden md:flex' : 'flex'}`}>
              <div className="flex items-center justify-between border-b border-border/60 px-3 py-1.5">
                <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {T('实时预览', 'Live preview')}
                </span>
                {saved && (
                  <span className="inline-flex items-center gap-1 text-[10px] font-medium text-emerald-600">
                    <CheckCircle2 className="h-3 w-3" />
                    {T('已保存', 'Saved')}
                  </span>
                )}
              </div>
              <div className="min-h-0 flex-1 bg-white">
                <iframe
                  title="artifact-canvas-preview"
                  sandbox="allow-scripts"
                  srcDoc={html}
                  className="h-full w-full border-0"
                />
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Footer: status + version trail */}
      {(lastError || saved || versions.length > 0) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border px-4 py-2">
          {lastError && (
            <span className="inline-flex items-center gap-1 text-[11px] text-red-600">
              <AlertCircle className="h-3 w-3" /> {lastError}
            </span>
          )}
          {saved && (
            <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600">
              <CheckCircle2 className="h-3 w-3" />
              {T('新版本已保存，预览与下载已更新', 'New version saved — preview and download updated')}
            </span>
          )}
          {versions.length > 0 && (
            <span className="ml-auto inline-flex items-center gap-1 text-[10px] text-muted-foreground">
              <RotateCcw className="h-3 w-3" />
              {versions.length} {T('个版本', 'versions')}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
