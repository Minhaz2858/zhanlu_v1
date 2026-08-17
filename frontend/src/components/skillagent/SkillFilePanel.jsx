import { useState, useMemo, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { File, Folder, FolderOpen, Search, Loader2, ChevronRight, ChevronDown, Download, FileJson, FileCode, FileText, X, Wrench, Activity, CheckCircle, AlertCircle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { listSkillExecutions } from '@/api/skillStudio';

function fileIcon(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (['md', 'markdown'].includes(ext)) return FileText;
  if (['json'].includes(ext)) return FileJson;
  if (['js', 'ts', 'py', 'sh', 'yaml', 'yml'].includes(ext)) return FileCode;
  return File;
}

function buildTree(skill, uploads) {
  const base = skill?.name || 'skill-workspace';
  const root = { name: base, type: 'folder', path: base, children: [] };
  if (skill) {
    root.children.push({
      name: 'SKILL.md', type: 'file', path: `${base}/SKILL.md`, ext: 'md',
      content: skill.skill_md || `# ${skill.name}\n\n${skill.description || ''}`,
    });
    const meta = {
      name: skill.name, kind: skill.kind, source: skill.source, status: skill.status,
      category: skill.category, version: skill.version, license: skill.license,
      trigger: skill.trigger, platform: skill.platform, sources: skill.sources, github_url: skill.github_url,
    };
    root.children.push({ name: '_meta.json', type: 'file', path: `${base}/_meta.json`, ext: 'json', content: JSON.stringify(meta, null, 2) });
    if (skill.references?.length) {
      const refs = { name: '_references', type: 'folder', path: `${base}/_references`, children: [] };
      skill.references.forEach((r) => refs.children.push({
        name: r.name, type: 'file', path: `${base}/_references/${r.name}`, ext: (r.name.split('.').pop() || '').toLowerCase(),
        content: r.content || '',
      }));
      root.children.push(refs);
    }
    // Folder-style package anatomy: references/*.md stored as a manifest of
    // filename -> summary (the full body lives on the backend filesystem).
    const refManifest = skill.references_manifest || skill.reference_manifest || {};
    const refKeys = Object.keys(refManifest).sort();
    if (refKeys.length) {
      const refs = { name: 'references', type: 'folder', path: `${base}/references`, children: [] };
      refKeys.forEach((fn) => refs.children.push({
        name: fn, type: 'file', path: `${base}/references/${fn}`, ext: (fn.split('.').pop() || '').toLowerCase(),
        content: refManifest[fn] ? `${refManifest[fn]}\n\n> Full content is stored with the skill package on disk.` : '',
      }));
      root.children.push(refs);
    }
    // assets/templates/* binary templates referenced for reuse on future runs.
    const assetManifest = skill.assets_manifest || {};
    const assetKeys = Object.keys(assetManifest).sort();
    if (assetKeys.length) {
      const assetsFolder = { name: 'assets', type: 'folder', path: `${base}/assets`, children: [] };
      const templatesFolder = { name: 'templates', type: 'folder', path: `${base}/assets/templates`, children: [] };
      assetKeys.forEach((rel) => {
        const clean = rel.replace(/^templates\//, '');
        templatesFolder.children.push({
          name: clean, type: 'file', path: `${base}/assets/templates/${clean}`, ext: (clean.split('.').pop() || '').toLowerCase(),
          content: assetManifest[rel] || '(binary template asset)', isAsset: true,
        });
      });
      assetsFolder.children.push(templatesFolder);
      root.children.push(assetsFolder);
    }
  }
  if (uploads.length) {
    const up = { name: 'uploads', type: 'folder', path: `${base}/uploads`, children: [] };
    uploads.forEach((f) => {
      const parts = (f.name || 'file').split('/').filter(Boolean);
      let cur = up;
      for (let i = 0; i < parts.length - 1; i++) {
        let ex = cur.children.find((c) => c.type === 'folder' && c.name === parts[i]);
        if (!ex) { ex = { name: parts[i], type: 'folder', path: `${cur.path}/${parts[i]}`, children: [] }; cur.children.push(ex); }
        cur = ex;
      }
      const fn = parts[parts.length - 1];
      cur.children.push({ name: fn, type: 'file', path: `${cur.path}/${fn}`, url: f.url, ext: (fn.split('.').pop() || '').toLowerCase(), content: '' });
    });
    root.children.push(up);
  }
  return root;
}

function flattenFiles(node, acc = []) {
  if (node.type === 'file') acc.push(node);
  else node.children?.forEach((c) => flattenFiles(c, acc));
  return acc;
}

function TreeNode({ node, depth, expanded, onToggle, onSelect, selectedPath, search }) {
  const isExpanded = expanded.has(node.path);
  const q = search.toLowerCase();
  if (node.type === 'folder') {
    const hasMatch = !q || flattenFiles(node).some((f) => f.name.toLowerCase().includes(q));
    if (q && !hasMatch) return null;
    return (
      <div>
        <button onClick={() => onToggle(node.path)} className="flex w-full items-center gap-1 py-1 pr-2 text-left hover:bg-secondary/50" style={{ paddingLeft: depth * 12 + 6 }}>
          {isExpanded ? <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" /> : <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />}
          {isExpanded ? <FolderOpen className="h-3.5 w-3.5 shrink-0 text-primary" /> : <Folder className="h-3.5 w-3.5 shrink-0 text-primary" />}
          <span className="truncate text-xs text-foreground">{node.name}</span>
        </button>
        {isExpanded && node.children?.map((c) => <TreeNode key={c.path} node={c} depth={depth + 1} expanded={expanded} onToggle={onToggle} onSelect={onSelect} selectedPath={selectedPath} search={search} />)}
      </div>
    );
  }
  if (q && !node.name.toLowerCase().includes(q)) return null;
  const Icon = fileIcon(node.name);
  return (
    <button onClick={() => onSelect(node.path)} className={`flex w-full items-center gap-1 py-1 pr-2 text-left ${selectedPath === node.path ? 'bg-primary/10 text-primary' : 'hover:bg-secondary/50'}`} style={{ paddingLeft: depth * 12 + 22 }}>
      <Icon className={`h-3.5 w-3.5 shrink-0 ${selectedPath === node.path ? 'text-primary' : 'text-muted-foreground'}`} />
      <span className="truncate text-xs">{node.name}</span>
    </button>
  );
}

export default function SkillFilePanel({ skill, messages, onClose }) {
  const { t } = useLanguage();
  const [selectedPath, setSelectedPath] = useState(null);
  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState(new Set());
  const [fileContent, setFileContent] = useState('');
  const [loadingContent, setLoadingContent] = useState(false);
  const [execStats, setExecStats] = useState(null);

  const uploads = useMemo(() => {
    const files = [];
    (messages || []).forEach((m) => {
      if (m.file_urls) {
        const urls = Array.isArray(m.file_urls) ? m.file_urls : [m.file_urls];
        urls.forEach((url) => {
          files.push({ name: decodeURIComponent(url.split('/').pop() || 'file'), url });
        });
      }
    });
    return files;
  }, [messages]);

  const tree = useMemo(() => buildTree(skill, uploads), [skill, uploads]);
  const allFiles = useMemo(() => (tree ? flattenFiles(tree) : []), [tree]);
  const selectedFile = allFiles.find((f) => f.path === selectedPath);

  useEffect(() => {
    if (tree) {
      setExpanded(new Set([tree.path, ...tree.children.filter((c) => c.type === 'folder').map((c) => c.path)]));
      const first = allFiles.find((f) => f.ext === 'md') || allFiles[0];
      if (first && !selectedPath) setSelectedPath(first.path);
    }
  }, [tree]);

  useEffect(() => {
    if (!selectedFile) { setFileContent(''); return; }
    if (selectedFile.content) { setFileContent(selectedFile.content); return; }
    if (selectedFile.url) {
      setLoadingContent(true);
      fetch(selectedFile.url).then((r) => r.text()).then((txt) => setFileContent(txt)).catch(() => setFileContent('')).finally(() => setLoadingContent(false));
    } else { setFileContent(''); }
  }, [selectedFile]);

  // Fetch execution stats for the current skill
  useEffect(() => {
    if (!skill?.name) { setExecStats(null); return; }
    let cancelled = false;
    listSkillExecutions(skill.name, { limit: 100 })
      .then((data) => {
        if (cancelled) return;
        const execs = data.executions || [];
        const total = execs.length;
        const completed = execs.filter((e) => e.status === 'completed').length;
        const failed = execs.filter((e) => e.status === 'failed').length;
        const loadExecs = execs.filter((e) => e.action === 'load' && typeof e.duration_ms === 'number');
        const avgLoadMs = loadExecs.length > 0
          ? Math.round(loadExecs.reduce((sum, e) => sum + e.duration_ms, 0) / loadExecs.length)
          : null;
        const successRate = total > 0 ? Math.round((completed / total) * 100) : 0;
        const lastExec = execs[0];
        const latestLoad = execs.find((e) => e.action === 'load' && (e.skill_id || e.skill_version || e.body_length));
        setExecStats({ total, completed, failed, successRate, avgLoadMs, lastExec, latestLoad });
      })
      .catch(() => { if (!cancelled) setExecStats(null); });
    return () => { cancelled = true; };
  }, [skill?.name]);

  function toggleFolder(path) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  }

  const isMarkdown = selectedFile?.ext === 'md' || selectedFile?.ext === 'markdown';

  return (
    <div className="flex h-full flex-col bg-card">
      <div className="flex items-center gap-1 border-b border-border px-2 py-1.5">
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-foreground">
          <Folder className="h-3.5 w-3.5" /> {t.skillAgent.files}
        </span>
        <button onClick={onClose} className="ml-auto inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Execution evidence badge */}
      {execStats && execStats.total > 0 && (
        <div className="flex items-center gap-3 border-b border-border bg-secondary/30 px-3 py-1.5">
          <div className="flex items-center gap-1">
            <Activity className="h-3 w-3 text-primary" />
            <span className="text-[10px] font-medium text-muted-foreground">{execStats.total} runs</span>
          </div>
          <div className="flex items-center gap-1">
            <CheckCircle className="h-3 w-3 text-green-500" />
            <span className="text-[10px] text-muted-foreground">{execStats.successRate}% success</span>
          </div>
          {execStats.failed > 0 && (
            <div className="flex items-center gap-1">
              <AlertCircle className="h-3 w-3 text-red-500" />
              <span className="text-[10px] text-muted-foreground">{execStats.failed} failed</span>
            </div>
          )}
          {execStats.lastExec && (
            <span className="ml-auto text-[10px] text-muted-foreground/70">
              {execStats.lastExec.action} · {execStats.lastExec.duration_ms ? `${execStats.lastExec.duration_ms}ms` : '—'}
            </span>
          )}
        </div>
      )}

      {execStats?.latestLoad && (
        <div className="flex flex-wrap items-center gap-2 border-b border-border bg-primary/5 px-3 py-1.5 text-[10px] text-muted-foreground">
          {execStats.latestLoad.skill_version && <span className="rounded-full bg-secondary px-2 py-0.5">v{execStats.latestLoad.skill_version}</span>}
          {execStats.latestLoad.skill_id && <span className="rounded-full bg-secondary px-2 py-0.5 font-mono">{execStats.latestLoad.skill_id}</span>}
          {execStats.latestLoad.body_length && <span className="rounded-full bg-secondary px-2 py-0.5">{execStats.latestLoad.body_length} chars</span>}
          {execStats.avgLoadMs != null && <span className="rounded-full bg-secondary px-2 py-0.5">avg load {execStats.avgLoadMs}ms</span>}
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        <div className="flex w-48 shrink-0 flex-col border-r border-border">
          <div className="border-b border-border p-2">
            <div className="flex items-center gap-1.5 rounded-md bg-secondary/50 px-2 py-1">
              <Search className="h-3 w-3 shrink-0 text-muted-foreground" />
              <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t.skillAgent.searchFiles} className="w-full bg-transparent text-xs text-foreground placeholder:text-muted-foreground focus:outline-none" />
            </div>
          </div>
          <div className="flex-1 overflow-y-auto py-1">
            {tree ? <TreeNode node={tree} depth={0} expanded={expanded} onToggle={toggleFolder} onSelect={setSelectedPath} selectedPath={selectedPath} search={search} /> : (
              <p className="px-3 py-4 text-center text-xs text-muted-foreground">{t.skillAgent.noFiles}</p>
            )}
          </div>
        </div>
        <div className="flex min-w-0 flex-1 flex-col">
          {selectedFile ? (
            <>
              <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
                <span className="truncate font-mono text-[11px] text-muted-foreground">{selectedFile.path}</span>
                {selectedFile.url && (
                  <a href={selectedFile.url} target="_blank" rel="noreferrer" className="ml-auto inline-flex items-center gap-1 text-[11px] text-primary hover:underline">
                    <Download className="h-3 w-3" /> {t.skillAgent.download}
                  </a>
                )}
              </div>
              <div className="flex-1 overflow-y-auto p-3">
                {loadingContent ? (
                  <div className="flex h-full items-center justify-center"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
                ) : isMarkdown ? (
                  <div className="prose prose-sm max-w-none">
                    <ReactMarkdown>{fileContent || ''}</ReactMarkdown>
                  </div>
                ) : (
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs text-foreground">{fileContent || ''}</pre>
                )}
              </div>
            </>
          ) : (
            <div className="flex h-full flex-col items-center justify-center text-center text-xs text-muted-foreground">
              <File className="mb-2 h-8 w-8 text-muted-foreground/40" />
              {t.skillAgent.noFileSelected}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}