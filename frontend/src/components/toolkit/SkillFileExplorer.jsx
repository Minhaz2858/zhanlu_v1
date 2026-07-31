import { useState } from 'react';
import { Folder, FileJson, FileText, ChevronRight, ChevronDown } from 'lucide-react';
import ReactMarkdown from 'react-markdown';

function buildMetaJson(tool) {
  return JSON.stringify({
    id: tool.id,
    name: tool.name,
    platform: tool.platform || 'minimax',
    skill_id: tool.skill_id || 10001,
    updated_at: tool.updated_date ? new Date(tool.updated_date).getTime() : Date.now(),
    version: tool.version || '1.0.0'
  }, null, 2);
}

function buildSkillMd(tool) {
  const sources = (tool.sources || []).filter(Boolean).map((s) => `- ${s}`).join('\n');
  const body = tool.skill_md || `# ${tool.name}\n\n${tool.description || ''}`;
  return `---
name: ${tool.name}
description: "${(tool.description || '').replace(/"/g, '\\"')}"
license: ${tool.license || 'MIT'}
metadata:
  version: "${tool.version || '1.0'}"
  category: ${tool.category || 'productivity'}
sources:
${sources || '  -'}
---

${body}`;
}

export default function SkillFileExplorer({ tool }) {
  const refs = tool.references || [];
  const [refsOpen, setRefsOpen] = useState(true);
  const [active, setActive] = useState('SKILL.md');

  const metaContent = buildMetaJson(tool);
  const skillContent = buildSkillMd(tool);

  const files = [
    { key: '_meta.json', icon: FileJson, content: metaContent, lang: 'json' },
    { key: 'SKILL.md', icon: FileText, content: skillContent, lang: 'markdown' },
    ...refs.map((r) => ({ key: `_references/${r.name}`, short: r.name, icon: FileText, content: r.content || '', lang: 'markdown' })),
  ];

  const current = files.find((f) => f.key === active) || files[0];

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-[#1e1e1e]">
      {/* Header bar */}
      <div className="flex items-center justify-between border-b border-[#333] bg-[#252525] px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm text-white">{tool.name}</span>
          <span className="text-xs text-[#888]">v{tool.version || '1.0.0'}</span>
        </div>
        <span className="rounded bg-[#37373d] px-2 py-0.5 text-[10px] text-[#ccc]">{tool.license || 'MIT'}</span>
      </div>

      <div className="flex h-[440px]">
        {/* File explorer */}
        <div className="w-56 shrink-0 overflow-y-auto border-r border-[#333] bg-[#252525] py-2">
          <div className="px-2">
            <div className="flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-[#ccc]">
              <Folder className="h-3.5 w-3.5 text-[#d4a044]" />
              <span className="truncate">{tool.name}</span>
            </div>
            <div className="ml-3 mt-0.5 space-y-0.5 border-l border-[#333] pl-2">
              <FileRow icon={FileJson} name="_meta.json" active={active === '_meta.json'} onClick={() => setActive('_meta.json')} />
              <FileRow icon={FileText} name="SKILL.md" active={active === 'SKILL.md'} onClick={() => setActive('SKILL.md')} />
              <button
                onClick={() => setRefsOpen((v) => !v)}
                className="flex w-full items-center gap-1 px-2 py-1 text-xs text-[#ccc] hover:text-white"
              >
                {refsOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                <Folder className="h-3.5 w-3.5 text-[#d4a044]" />
                <span>_references</span>
              </button>
              {refsOpen && (
                <div className="ml-3 space-y-0.5 border-l border-[#333] pl-2">
                  {refs.length === 0 ? (
                    <div className="px-2 py-1 text-[10px] text-[#666]">— empty —</div>
                  ) : (
                    refs.map((r) => {
                      const k = `_references/${r.name}`;
                      return <FileRow key={k} icon={FileText} name={r.name} active={active === k} onClick={() => setActive(k)} />;
                    })
                  )}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Content pane */}
        <div className="flex-1 overflow-auto bg-[#1e1e1e]">
          <div className="border-b border-[#333] bg-[#252525] px-4 py-1.5">
            <span className="font-mono text-xs text-[#ccc]">{current.short || current.key}</span>
          </div>
          {current.lang === 'markdown' ? (
            <div className="px-5 py-4 text-sm leading-relaxed text-[#ececec]">
              <ReactMarkdown
                components={{
                  h1: ({ node, ...p }) => <h1 className="mb-3 mt-2 text-lg font-semibold text-white" {...p} />,
                  h2: ({ node, ...p }) => <h2 className="mb-2 mt-4 text-base font-semibold text-white" {...p} />,
                  p: ({ node, ...p }) => <p className="mb-2 text-[#ccc]" {...p} />,
                  code: ({ node, ...p }) => <code className="rounded bg-[#2a2a2a] px-1.5 py-0.5 font-mono text-xs text-[#4fc1ff]" {...p} />,
                  a: ({ node, ...p }) => <a className="text-[#3b82f6] underline" target="_blank" rel="noreferrer" {...p} />,
                  table: ({ node, ...p }) => <table className="my-3 w-full border-collapse text-xs" {...p} />,
                  th: ({ node, ...p }) => <th className="border border-[#333] bg-[#2a2a2a] px-2 py-1 text-left text-white" {...p} />,
                  td: ({ node, ...p }) => <td className="border border-[#333] px-2 py-1 text-[#ccc]" {...p} />,
                }}
              >
                {current.content}
              </ReactMarkdown>
            </div>
          ) : (
            <pre className="px-5 py-4 font-mono text-xs leading-relaxed text-[#ccc] whitespace-pre-wrap">{current.content}</pre>
          )}
        </div>
      </div>
    </div>
  );
}

function FileRow({ icon: Icon, name, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center gap-1.5 px-2 py-1 text-xs transition-colors ${active ? 'bg-[#37373d] text-white' : 'text-[#ccc] hover:bg-[#2a2a2a]'}`}
    >
      <Icon className="h-3.5 w-3.5 shrink-0 text-[#d4a044]" />
      <span className="truncate">{name}</span>
    </button>
  );
}