import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

function isTableRow(line) {
  return line.trim().startsWith('|');
}

function isSeparator(line) {
  const t = line.trim();
  return /^\|?[\s:?-]*-+[\s:?-]*(\|[\s:?-]*-+[\s:?-]*)*\|?$/.test(t);
}

function parseRow(line) {
  let t = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  return t.split('|').map((c) => c.trim());
}

function splitContent(content) {
  const lines = content.split('\n');
  const segments = [];
  let buffer = [];
  let inTable = false;
  let inOptions = false;

  function flushTable() {
    if (!buffer.length) return;
    if (buffer.some((l) => isSeparator(l))) {
      segments.push({ type: 'table', content: buffer.join('\n') });
    } else {
      segments.push({ type: 'md', content: buffer.join('\n') });
    }
    buffer = [];
  }

  function flushMd() {
    if (!buffer.length) return;
    segments.push({ type: 'md', content: buffer.join('\n') });
    buffer = [];
  }

  for (const line of lines) {
    if (line.trim().startsWith(':::options')) {
      if (inTable) { flushTable(); inTable = false; }
      else { flushMd(); }
      inOptions = true;
      continue;
    }
    if (inOptions && line.trim() === ':::') {
      segments.push({ type: 'options', content: buffer.join('\n') });
      buffer = [];
      inOptions = false;
      continue;
    }
    if (inOptions) {
      buffer.push(line);
      continue;
    }
    if (isTableRow(line)) {
      if (!inTable) { flushMd(); inTable = true; }
      buffer.push(line);
    } else {
      if (inTable) { flushTable(); inTable = false; }
      buffer.push(line);
    }
  }
  if (inOptions) segments.push({ type: 'options', content: buffer.join('\n') });
  else if (inTable) flushTable();
  else flushMd();
  return segments;
}

function TableBlock({ content }) {
  const lines = content.split('\n').filter((l) => l.trim());
  const sepIdx = lines.findIndex((l) => isSeparator(l));
  if (sepIdx < 1) return <pre className="mb-3 overflow-x-auto text-xs">{content}</pre>;
  const headers = parseRow(lines[sepIdx - 1]);
  const dataLines = lines.slice(sepIdx + 1).filter((l) => isTableRow(l));
  const rows = dataLines.map(parseRow);
  return (
    <div className="mb-3 overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="bg-secondary">
            {headers.map((h, i) => (
              <th key={i} className="border border-border px-3 py-1.5 text-left font-medium text-foreground">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="even:bg-secondary/30">
              {row.map((cell, j) => (
                <td key={j} className="border border-border px-3 py-1.5 text-muted-foreground">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OptionsBlock({ content, onSelect, multiSelect = false }) {
  const options = content.split('\n').map((l) => l.trim()).filter(Boolean);
  if (!options.length) return null;
  const [selected, setSelected] = useState(() => new Set());

  if (!multiSelect) {
    // Back-compat: single-click UX for Agent Builder / Embedded Agent Builder.
    return (
      <div className="mb-3 mt-1 flex flex-wrap gap-2" data-multiselect="false">
        {options.map((opt, i) => (
          <button
            key={i}
            type="button"
            onClick={() => onSelect?.(opt)}
            className="rounded-full border border-primary/30 bg-primary/5 px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary hover:text-primary-foreground"
          >
            {opt}
          </button>
        ))}
      </div>
    );
  }

  // Multi-select UX for Skill Agent bare-request narrowing.
  const containerRef = useRef(null);
  const toggle = (opt) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(opt)) next.delete(opt);
      else next.add(opt);
      return next;
    });
  };
  const commit = () => {
    const text = Array.from(selected).join(', ');
    onSelect?.(text);
    setSelected(new Set()); // reset for the next :::options block
  };
  const count = selected.size;
  // 2026-07-28: Scroll the multi-select block into view when it's
  // rendered. Without this, the chips can be hidden behind the sticky
  // chat header (especially the first message in a conversation), and
  // the user sees the "Use these (N)" button without the chips above
  // it. block: 'center' puts the block in the middle of the viewport.
  useEffect(() => {
    if (!containerRef.current) return;
    containerRef.current.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, []);
  return (
    <div ref={containerRef} className="mb-3 mt-1 scroll-mt-20" data-multiselect="true">
      <div
        role="group"
        aria-label="Multi-select options"
        className="flex flex-wrap gap-2"
      >
        {options.map((opt, i) => {
          const isSelected = selected.has(opt);
          return (
            <button
              key={i}
              type="button"
              role="checkbox"
              aria-checked={isSelected}
              aria-pressed={isSelected}
              onClick={() => toggle(opt)}
              className={
                isSelected
                  ? 'rounded-full border border-primary bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors'
                  : 'rounded-full border border-primary/30 bg-primary/5 px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary/10'
              }
            >
              {opt}
            </button>
          );
        })}
      </div>
      <div className="mt-2 flex justify-end">
        <button
          type="button"
          onClick={commit}
          disabled={count === 0}
          className={
            count === 0
              ? 'cursor-not-allowed rounded-full border border-border bg-secondary px-3 py-1 text-xs font-medium text-muted-foreground'
              : 'rounded-full border border-primary bg-primary px-3 py-1 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90'
          }
        >
          Use these ({count})
        </button>
      </div>
    </div>
  );
}

const mdComponents = {
  p: ({ node, ...p }) => <p className="mb-3 last:mb-0 leading-relaxed" {...p} />,
  ul: ({ node, ...p }) => <ul className="mb-3 list-disc space-y-1 pl-5" {...p} />,
  ol: ({ node, ...p }) => <ol className="mb-3 list-decimal space-y-1 pl-5" {...p} />,
  li: ({ node, ...p }) => <li className="leading-relaxed" {...p} />,
  code: ({ className, children, ...props }) => {
    if (className) return <code className={`font-mono text-xs ${className}`} {...props}>{children}</code>;
    return <code className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[11px]" {...props}>{children}</code>;
  },
  pre: ({ node, ...p }) => <pre className="mb-3 overflow-x-auto rounded-lg bg-secondary p-3 text-xs" {...p} />,
  h1: ({ node, ...p }) => <h1 className="mb-2 mt-1 font-display text-lg font-semibold" {...p} />,
  h2: ({ node, ...p }) => <h2 className="mb-2 mt-3 font-display text-base font-semibold" {...p} />,
  h3: ({ node, ...p }) => <h3 className="mb-1.5 mt-2 font-display text-sm font-semibold" {...p} />,
  h4: ({ node, ...p }) => <h4 className="mb-1 mt-2 text-sm font-medium" {...p} />,
  strong: ({ node, ...p }) => <strong className="font-semibold text-foreground" {...p} />,
  em: ({ node, ...p }) => <em className="italic" {...p} />,
  blockquote: ({ node, ...p }) => <blockquote className="mb-3 border-l-2 border-primary/40 pl-3 italic text-muted-foreground" {...p} />,
  hr: ({ node, ...p }) => <hr className="my-3 border-border" {...p} />,
  a: ({ node, ...p }) => <a className="text-primary underline hover:opacity-80" target="_blank" rel="noreferrer" {...p} />,
};

export default function AgentMarkdown({ children, className, onOptionSelect, multiSelect = false }) {
  if (typeof children !== 'string') {
    return <div className={className}>{children}</div>;
  }
  const segments = splitContent(children);
  return (
    <div className={className}>
      {segments.map((seg, i) => {
        if (seg.type === 'table') return <TableBlock key={i} content={seg.content} />;
        if (seg.type === 'options') return <OptionsBlock key={i} content={seg.content} onSelect={onOptionSelect} multiSelect={multiSelect} />;
        return <ReactMarkdown key={i} remarkPlugins={[remarkGfm]} components={mdComponents}>{seg.content}</ReactMarkdown>;
      })}
    </div>
  );
}