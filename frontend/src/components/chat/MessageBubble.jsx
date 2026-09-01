import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import StreamingMarkdown from './StreamingMarkdown';
import { useState, useCallback, useEffect, useRef } from 'react';
import { Bot, User, Eye, Copy, Check, Play, Settings, ArrowRight, ChevronDown, ChevronUp, Pencil, Database, Table2, FileText, FileSpreadsheet, Image as ImageIcon, File, ExternalLink } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import ClarifyOptions from './ClarifyOptions';
import ClarifyBatchForm from './ClarifyBatchForm';
import LiveActivityStream from './LiveActivityStream';
import ResultCard from './ResultCard';
import DataTableCard from './DataTableCard';
import ReportCard from './ReportCard';
import ArtifactPreviewCard from './ArtifactPreviewCard';
import ArtifactCardList from './ArtifactCardList';
import InlineArtifactPreview from './InlineArtifactPreview';
import { partitionArtifacts } from './partitionArtifacts';
import DashboardCard from '@/components/dashboard/DashboardCard';
import MessageActions from './MessageActions';

/**
 * Pre-process markdown content to fix common LLM formatting issues.
 *
 * 1. Pipe-reflow: when the LLM emits a single line like
 *    ``| H1 | H2 | | row1 | row2 |`` we split on `` | `` sequences
 *    and re-join with ``\n`` so remark-gfm sees real table rows.
 * 2. Collapses excessive blank lines (>2 consecutive).
 */
function preProcessContent(text) {
  if (!text) return text;

  let result = text;

  // --- Inline-header reflow (2026-08-28) ---
  // When a line has leading prose glued to a 3+ cell pipe-table header
  // (e.g. "Total revenue: ¥X. | Product | Units | Revenue | ...")
  // GFM refuses to recognise the table because the header is part of
  // a paragraph. Split before the first pipe so the header starts its
  // own line and GFM picks it up. Existing single-line reflow (next
  // block) only fires when there are NO \n-|-prefixed lines, so this
  // case was previously missed.
  result = result.replace(
    /^([^\n|]+?)(\s*\|\s*[^\n|]+\s*(?:\|\s*[^\n|]+\s*){2,}\|[^\n]*)$/gm,
    (_match, prefix, tablePart) => `${prefix.trimEnd()}\n${tablePart.trimStart()}`,
  );

  // --- Pipe-reflow: detect inline tables on a single line ---
  // A real markdown table has a separator row (|---|---|).
  // If we find pipe chars but no newline + pipe (which would indicate
  // proper row separation), insert newlines.
  if (/\|.*\|/.test(result) && !/\n\s*\|/.test(result)) {
    // Split on " | " boundary between cells — this turns
    // "| H1 | H2 | | row | row |" into proper rows.
    // Strategy: find all content between pipe-delimiter boundaries
    // and rebuild with newlines.
    const lines = [];
    const cells = result
      .split(/\s*\|\s*/)
      .map((c) => c.trim())
      .filter(Boolean);
    if (cells.length >= 3) {
      // First row: headers
      const colCount = Math.min(
        cells.filter((c) => !/^-{3,}$/.test(c)).length,
        6,
      );
      let row = [];
      for (let i = 0; i < cells.length; i++) {
        row.push(cells[i]);
        if (row.length >= colCount) {
          lines.push(`| ${row.join(' | ')} |`);
          row = [];
        }
      }
      if (row.length > 0) {
        lines.push(`| ${row.join(' | ')} |`);
      }
      if (lines.length >= 2) {
        // Insert a header separator row after the first line if none exists
        const hasSeparator = /^\|[\s\-:|]+\|$/.test(lines[1] || '');
        if (!hasSeparator) {
          const sep = `| ${Array(colCount).fill('---').join(' | ')} |`;
          lines.splice(1, 0, sep);
        }
        result = lines.join('\n');
      }
    }
  }

  // --- Collapse excessive blank lines ---
  result = result.replace(/\n{3,}/g, '\n\n');

  return result;
}

/**
 * Pick an icon for an attachment chip based on its file name/extension.
 * Mirrors ChatInput's attachmentIcon so history cards look identical to
 * the draft chips shown before send.
 */
function attachmentIcon(name) {
  const ext = (name || '').split('.').pop().toLowerCase();
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'tiff', 'tif'].includes(ext)) return ImageIcon;
  if (['xlsx', 'xls', 'csv', 'tsv'].includes(ext)) return FileSpreadsheet;
  if (['pdf', 'docx', 'pptx', 'ppt', 'txt', 'md', 'json', 'html', 'htm'].includes(ext)) return FileText;
  return File;
}

/** Inline code copy button hook */
function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => { });
  }, [text]);
  return (
    <button
      onClick={handleCopy}
      className="absolute right-2 top-2 rounded-md bg-white/10 p-1 text-white/60 transition-colors hover:bg-white/20 hover:text-white"
      title={copied ? 'Copied!' : 'Copy code'}
    >
      {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

const components = {
  p: ({ node, ...props }) => <p className="mb-2 last:mb-0 leading-relaxed" {...props} />,
  ul: ({ node, ...props }) => <ul className="mb-2 list-disc space-y-1 pl-4" {...props} />,
  ol: ({ node, ...props }) => <ol className="mb-2 list-decimal space-y-1 pl-4" {...props} />,
  li: ({ node, ...props }) => <li {...props} />,
  // Inline code
  code: ({ node, className, children, ...props }) => {
    const isInline = !className;
    if (isInline) {
      return (
        <code className="rounded bg-secondary px-1.5 py-0.5 font-mono text-xs text-primary" {...props}>
          {children}
        </code>
      );
    }
    // Fenced code block — rendered inside <pre>
    const match = /language-(\w+)/.exec(className || '');
    const codeText = String(children).replace(/\n$/, '');
    return (
      <div className="group relative my-2">
        {match && (
          <span className="absolute left-3 top-2 text-[10px] font-medium uppercase tracking-wide text-white/40">
            {match[1]}
          </span>
        )}
        <CopyButton text={codeText} />
        <code className={`block overflow-x-auto rounded-lg bg-[#1e1e2e] p-4 pt-7 font-mono text-xs leading-relaxed text-[#cdd6f4] ${className || ''}`} {...props}>
          {children}
        </code>
      </div>
    );
  },
  pre: ({ node, ...props }) => <>{props.children}</>,
  // Headings
  h1: ({ node, ...props }) => <h1 className="mb-3 mt-4 font-display text-lg font-semibold tracking-tight text-foreground first:mt-0" {...props} />,
  h2: ({ node, ...props }) => <h2 className="mb-2 mt-4 font-display text-base font-semibold text-foreground first:mt-0" {...props} />,
  h3: ({ node, ...props }) => <h3 className="mb-1 mt-3 font-display text-sm font-semibold text-foreground first:mt-0" {...props} />,
  // Blockquote
  blockquote: ({ node, ...props }) => (
    <blockquote
      className="my-2 border-l-[3px] border-primary/40 bg-secondary/40 px-3 py-1.5 text-sm italic text-muted-foreground"
      {...props}
    />
  ),
  // Table components
  table: ({ node, ...props }) => (
    <div className="my-3 overflow-x-auto rounded-lg border border-border">
      <table className="min-w-full border-collapse text-xs" {...props} />
    </div>
  ),
  thead: ({ node, ...props }) => <thead className="bg-secondary" {...props} />,
  tbody: ({ node, ...props }) => <tbody {...props} />,
  tr: ({ node, isHeader, ...props }) => {
    const base = 'border-b border-border';
    return <tr className={`${base} ${isHeader ? '' : 'even:bg-secondary/30'}`} {...props} />;
  },
  th: ({ node, ...props }) => (
    <th
      className="whitespace-nowrap px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wider text-muted-foreground"
      {...props}
    />
  ),
  td: ({ node, ...props }) => (
    <td className="px-3 py-1.5 text-xs text-foreground" {...props} />
  ),
  // Horizontal rule
  hr: ({ node, ...props }) => <hr className="my-4 border-border" {...props} />,
  // Strong / emphasis
  strong: ({ node, ...props }) => <strong className="font-semibold text-foreground" {...props} />,
  em: ({ node, ...props }) => <em className="italic" {...props} />,
  // Links
  a: ({ node, ...props }) => (
    <a className="break-all text-primary underline decoration-primary/30 underline-offset-2 transition-colors hover:decoration-primary" target="_blank" rel="noopener noreferrer" {...props} />
  ),
  // Images
  img: ({ node, ...props }) => (
    <img className="my-2 max-h-80 rounded-lg border border-border object-contain" loading="lazy" {...props} />
  ),
  // Task list items (from remark-gfm)
  input: ({ node, checked, ...props }) => {
    // remark-gfm emits <input type="checkbox"> for task lists
    return (
      <input
        type="checkbox"
        checked={checked}
        readOnly
        className="mr-2 h-3.5 w-3.5 rounded border-border accent-primary"
        {...props}
      />
    );
  },
};

function parseContent(content) {
  const parts = [];
  const regex = /\[\[(CLARIFY_BATCH|CLARIFY|RESULT)\]\]\s*\n?([\s\S]*?)\[\[END\]\]/g;
  let lastIndex = 0;
  let match;
  while ((match = regex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      const text = content.slice(lastIndex, match.index).trim();
      if (text) parts.push({ type: 'text', text });
    }
    const tag = match[1];
    let block = null;
    try { block = JSON.parse(match[2].trim()); } catch { block = null; }
    if (tag === 'CLARIFY_BATCH' && block && Array.isArray(block.questions)) parts.push({ type: 'clarify_batch', block });
    else if (tag === 'CLARIFY' && block && block.prompt) parts.push({ type: 'clarify', block });
    else if (tag === 'RESULT' && block && (block.id || block.draft)) parts.push({ type: 'result', block });
    else parts.push({ type: 'text', text: match[0] });
    lastIndex = regex.lastIndex;
  }
  if (lastIndex < content.length) {
    const text = content.slice(lastIndex).trim();
    if (text) parts.push({ type: 'text', text });
  }
  return parts;
}

export default function MessageBubble({ message, isStreaming, onSelectOption, onSelectOther, onPreview, onArtifactPreview, onArtifactEdit, onBatchClarifySubmit, onAgentPreview, onAgentRun, onOpenRun, userRequestedFormat, onEditMessage, onFeedback, feedbackRating, showRoleRating, onRoleRelevance, roleRelevanceRating, onRegenerate, onPreviewFile }) {
  const { lang } = useLanguage();
  const isUser = message.role === 'user';
  const parts = isUser ? null : parseContent(message.content || '');
  const hasReasoning = !isUser && typeof message.reasoning === 'string' && message.reasoning.length > 0;
  const htmlMatch = !isUser && !isStreaming ? (message.content || '').match(/```html\s*([\s\S]*?)```/i) : null;
  const hasResult = parts?.some((part) => part.type === 'result');
  // Split artifacts into the two disjoint render surfaces below:
  // automation deliverables / previewable files get the Manus-style
  // inline preview; ordinary chat-only artifacts get the compact card
  // list. Passing the full list to both rendered the same file twice.
  const { inline: inlineArtifacts, cards: cardArtifacts } = partitionArtifacts(message.artifacts);

  // Detect ask_data_agent tool calls with rows in message.tool_calls.
  // Recognize the direct-SQL tool family too, and prefer an answer-tagged
  // dataset for the inline table — probe-tagged results (reference lookups
  // / ID samples) are skipped so raw probe rows don't leak into the final
  // answer (2026-08-21).
  const dataToolCandidates = !isUser && Array.isArray(message.tool_calls)
    ? message.tool_calls.filter((tc) => {
      if (!tc || !tc.results) return false;
      const name = tc.name || tc.tool_name || '';
      const isDataTool = name === 'ask_data_agent'
        || name === 'execute_query'
        || name === 'execute_sql'
        || name === 'sql_query'
        || name === 'Database Query';
      const hasRows = Array.isArray(tc.results.rows) && tc.results.rows.length > 0;
      return isDataTool && hasRows;
    })
    : [];
  const dataToolResult = dataToolCandidates.find(
    (tc) => (tc.results && tc.results.query_purpose) !== 'probe',
  ) || null;

  // Detect Synexia report-card payload (preferred over DataTableCard for reports)
  const reportToolResult = !isUser && Array.isArray(message.tool_calls)
    ? message.tool_calls.find((tc) => {
      if (!tc || !tc.results) return false;
      const payload = tc.results.report_card_payload;
      if (!payload || typeof payload !== 'object') return false;
      // Either explicitly typed by the backend, or has the rich payload shape
      return tc.results.type === 'report_card' || (payload.title && (payload.kpis || payload.chart || payload.insights));
    })
    : null;

  // Detect a formal file deliverable on this message (docx/pptx/pdf/xlsx/html/md).
  // When the agent chose to produce a polished file we suppress the raw
  // `ask_data_agent` rows card so the deliverable is the single visible
  // surface — the user explicitly asked for a file, not a warehouse preview.
  // (2026-08-28 UX fix: prior to this, "give me last month sales report in
  // docx file" rendered BOTH the 175×15 raw table AND the DOCX card.)
  const DELIVERABLE_TYPES = new Set(['docx', 'pptx', 'pdf', 'xlsx', 'html', 'md', 'html_report']);
  const hasDeliverableArtifact = Array.isArray(message.artifacts)
    && message.artifacts.some((a) => {
      const t = (a?.type || a?.file_type || a?.artifact_type || '').toLowerCase();
      return DELIVERABLE_TYPES.has(t);
    });

  // Artifact previews are NOT auto-opened: the user explicitly clicks
  // "Open" on the artifact card (ArtifactPreviewCard) to open the
  // right-anchored preview pane.

  // Pending export in progress — user_signal starts with export_ but
  // the sandbox hasn't returned file_exports yet.
  const isPendingExport = !!(reportToolResult?.results?.user_signal?.startsWith('export_')) && !reportToolResult?.results?.file_exports;

  const isExportSignal = (signal) =>
    signal === 'export' ||
    signal === 'download' ||
    signal === 'save' ||
    signal === 'export_docx' ||
    signal === 'export_pptx' ||
    signal === 'export_xlsx' ||
    signal === 'export_pdf' ||
    signal === 'export_md';

  // Compact user bubble: by default we render a single-line summary so
  // long user inputs don't push the agent's response off-screen. The
  // user can click "show more" to expand the full content inline. The
  // full text is also available on hover via the title attribute, so
  // the user never loses information.
  const rawContent = message.content || '';
  const oneLine = rawContent.replace(/\s+/g, ' ').trim();
  const [userExpanded, setUserExpanded] = useState(false);
  // Ref for the assistant message content — used by html2canvas in MessageActions
  const contentRef = useRef(null);
  // 120 chars matches the typical "one line" feel on a max-w-[90%] bubble
  // at desktop widths without being so short that the truncation is jarring.
  const USER_PREVIEW_LIMIT = 120;
  const needsTruncation = oneLine.length > USER_PREVIEW_LIMIT || rawContent.includes('\n');
  const previewText = oneLine.length > USER_PREVIEW_LIMIT
    ? `${oneLine.slice(0, USER_PREVIEW_LIMIT).trimEnd()}…`
    : oneLine;
  // Pull any attachments (file chips) off the message so the compact
  // summary can show a small badge like "📎 2 files" next to the text.
  const attachmentList = Array.isArray(message.attachments) ? message.attachments : [];
  const attachmentBadge = attachmentList.length > 0
    ? (lang === 'en'
      ? `📎 ${attachmentList.length} file${attachmentList.length === 1 ? '' : 's'}`
      : `📎 ${attachmentList.length} 个附件`)
    : null;

  return (
    <div data-testid={isUser ? 'msg-user' : 'msg-assistant'} className={`flex animate-slide-up gap-3 ${isUser ? 'flex-row-reverse' : ''}`}>
      <div
        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${isUser
            ? 'bg-primary text-primary-foreground'
            : 'border border-border bg-secondary text-muted-foreground'
          }`}
      >
        {isUser ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
      </div>
      <div className={`flex max-w-[90%] flex-col ${isUser ? 'items-end' : 'items-start'}`}>
        {isUser ? (
          <div className="group flex items-start gap-2">
            {/* Edit button — appears on hover for user messages with the branching callback.
                Rendered as a flex sibling (not absolutely positioned) so there is no
                gap between the bubble and the button — the parent group-hover state
                stays active as the cursor moves toward the icon. */}
            {onEditMessage && !isStreaming && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onEditMessage(message); }}
                title={lang === 'en' ? 'Edit & resend' : '编辑并重发'}
                className="mt-1 inline-flex shrink-0 rounded-md p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-secondary hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100"
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
            )}
            <div
              className="max-w-full rounded-2xl rounded-tr-sm bg-secondary px-4 py-2.5 text-sm text-foreground"
              title={needsTruncation ? rawContent : undefined}
            >
              {/* Attached files (Kimi/ChatGPT-style): the upload chip shown
                  in the input becomes a clickable card in chat history so
                  the user can see which file the message carried and open
                  it. Rendered above the text, like ChatGPT/Kimi. */}
              {attachmentList.length > 0 && (
                <div className="mb-1.5 flex flex-col items-start gap-1">
                  {attachmentList.map((att, i) => {
                    const attName = att?.name || att?.file_url || '';
                    const Icon = attachmentIcon(attName);
                    return (
                      <a
                        key={i}
                        href={att?.file_url}
                        target="_blank"
                        rel="noreferrer"
                        onClick={(e) => {
                          // Kimi/ChatGPT-style: open the file in the right
                          // preview pane when the parent provides the
                          // callback; otherwise fall back to the default
                          // new-tab navigation (e.g. mobile has no pane).
                          if (onPreviewFile && att?.file_url) {
                            e.preventDefault();
                            onPreviewFile(att);
                          }
                          e.stopPropagation();
                        }}
                        className="inline-flex max-w-full items-center gap-2 rounded-lg border border-border bg-card/70 px-2.5 py-1.5 text-xs text-foreground transition-colors hover:bg-secondary"
                        title={att?.file_url}
                        data-testid="msg-attachment-chip"
                      >
                        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                        <span className="truncate">{attName}</span>
                        <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground/70" />
                      </a>
                    );
                  })}
                </div>
              )}
              {userExpanded ? (
                <div className="whitespace-pre-wrap break-words">{rawContent}</div>
              ) : (
                <div className="truncate text-left" dir="auto">{previewText}</div>
              )}
              {(attachmentList.length > 0 || needsTruncation) && (
                <div className="mt-1.5 flex items-center justify-end gap-2 text-[11px] text-muted-foreground">
                  {attachmentBadge && <span>{attachmentBadge}</span>}
                  {needsTruncation && (
                    <button
                      type="button"
                      onClick={() => setUserExpanded((v) => !v)}
                      className="inline-flex items-center gap-0.5 rounded-full bg-secondary/60 px-2 py-0.5 font-medium text-foreground/80 transition-colors hover:bg-secondary hover:text-foreground"
                    >
                      {userExpanded ? (
                        <>
                          {lang === 'en' ? 'Show less' : '收起'}
                          <ChevronUp className="h-3 w-3" />
                        </>
                      ) : (
                        <>
                          {lang === 'en' ? 'Show more' : '展开'}
                          <ChevronDown className="h-3 w-3" />
                        </>
                      )}
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
        ) : (
          <div ref={contentRef} className="min-w-0 break-words rounded-2xl rounded-tl-sm border border-border bg-card px-4 py-2.5 text-sm whitespace-pre-wrap text-foreground">
            {/* Unified live activity feed (2026-08-22). The component itself
                synthesizes `live_events` from legacy `activity_steps` when
                present, so every assistant message — past or future — renders
                through this single component with one visual design. */}
            <LiveActivityStream
              events={message.live_events}
              legacySteps={message.activity_steps}
              phase={message.phase}
              reasoning={hasReasoning ? message.reasoning : undefined}
              isStreaming={isStreaming}
              streamingSearchQueries={message.streaming_search_queries}
              streamingPlanSteps={message.streaming_plan_steps}
              streamingDataPreviews={message.streaming_data_previews}
              streamingReasoning={message.streaming_reasoning}
              streamingAction={message.streaming_action}
            />
            {message.phase?.execution_id && onOpenRun && (
              <button
                type="button"
                onClick={() => onOpenRun(message.phase.execution_id)}
                className="mb-2 inline-flex items-center gap-1 rounded-md border border-primary/30 bg-primary/5 px-2 py-1 text-[11px] font-medium text-primary transition-colors hover:bg-primary/10"
              >
                <ArrowRight className="h-3 w-3" />
                Open run
              </button>
            )}
            {/* ReportCard and ArtifactPreviewCard removed — inline markdown
                report is the primary deliverable; the preview card was
                redundant and cluttered the chat. */}
            {/* Suppress raw DataTableCard while the message is still streaming
                AND when the message already carries a substantive synthesized
                answer (markdown section headers signal a real inline report).
                Raw data stays visible inside the collapsible activity steps
                instead. Mid-flight: only step indicators should show. */}
            {!isStreaming && !reportToolResult && dataToolResult && !hasDeliverableArtifact && !(/##\s+/.test(message.content || '')) && (
              <DataTableCard
                rows={dataToolResult.results.rows}
                sql={dataToolResult.results.sql}
                source={dataToolResult.results.source_name}
              />
            )}
            {parts.map((p, i) =>
              p.type === 'clarify_batch' ? (
                <ClarifyBatchForm key={i} block={p.block} onSubmit={onBatchClarifySubmit} />
              ) : p.type === 'clarify' ? (
                <ClarifyOptions key={i} block={p.block} onSelectOption={onSelectOption} onSelectOther={onSelectOther} />
              ) : p.type === 'result' ? (
                <div key={i}>
                  <ResultCard result={p.block} onPreview={onPreview} />
                  {p.block.type === 'file' && p.block.id && (
                    <ArtifactPreviewCard artifactId={p.block.id} onOpen={onArtifactPreview} />
                  )}
                  {/* Preview + Run buttons when an agent was created via chat */}
                  {p.block.type === 'agent' && p.block.id && (
                    <div className="mt-2 flex gap-2">
                      <button
                        onClick={() => onAgentPreview?.(p.block.id)}
                        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
                      >
                        <Settings className="h-3.5 w-3.5" /> {lang === 'en' ? 'Preview' : '预览'}
                      </button>
                      <button
                        onClick={() => onAgentRun?.(p.block.id)}
                        className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90"
                      >
                        <Play className="h-3.5 w-3.5" /> {lang === 'en' ? 'Run' : '运行'} <ArrowRight className="h-3 w-3" />
                      </button>
                    </div>
                  )}
                </div>
              ) : (
                // 2026-08-25: activity-feed modernization task 11.
                // Use StreamingMarkdown for the streaming case to avoid
                // O(N²) re-parse on every token. Falls back to the
                // existing react-markdown for the non-streaming case.
                isStreaming ? (
                  <StreamingMarkdown content={preProcessContent(p.text)} isStreaming={true} components={components} />
                ) : (
                  <ReactMarkdown key={i} remarkPlugins={[remarkGfm]} components={components}>
                    {preProcessContent(p.text)}
                  </ReactMarkdown>
                )
              )
            )}
            {isStreaming && (
              <span className="ml-0.5 inline-block h-4 w-2 animate-pulse rounded-sm bg-primary align-text-bottom" />
            )}
            {/* 2026-08-25: subtle "Refining answer..." indicator when the
                server emitted a content_preserve SSE event. The old text
                stays visible (no collapse); this just signals that a
                replacement is streaming in. */}
            {!isUser && message.refining && (
              <div className="mt-2 flex items-center gap-2 text-xs italic text-muted-foreground">
                <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
                {lang === 'en' ? 'Refining answer…' : '正在优化答案…'}
              </div>
            )}
            {htmlMatch && !hasResult && (
              <button onClick={() => onPreview?.({ type: 'file', name: 'HTML Preview', draft: true, fields: { file_type: 'html', html_content: htmlMatch[1].trim(), source: 'ai_generated', resource_kind: 'html_file' } })} className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-border bg-secondary/40 px-2.5 py-1.5 text-xs font-medium text-foreground hover:bg-secondary">
                <Eye className="h-3.5 w-3.5" /> {lang === 'en' ? 'Preview HTML in Artifacts' : '在产物中预览 HTML'}
              </button>
            )}
            {/** Artifact cards from create_artifact tool results — only
             * the non-inline subset (see partitionArtifacts); inline-bound
             * artifacts render below via InlineArtifactPreview.
             * Suppress while streaming to prevent mid-flight artifact card
             * leaks (e.g. "Database inventory — schema overview" cards). */}
            {!isStreaming && (
              <ArtifactCardList artifacts={cardArtifacts} onPreview={onArtifactPreview} onEdit={onArtifactEdit} userRequestedFormat={userRequestedFormat} />
            )}
            {/**
             * Manus-style inline deliverable: when a chat message attaches a
             * generated automation file (e.g. an HTML report dropped into the
             * conversation by a scheduled run), render it inline as a card +
             * expandable preview so the user sees the deliverable next to the
             * assistant turn that produced it.  Falls back to ArtifactCardList
             * for ordinary chat-only artifacts.
             * Suppress while streaming to prevent mid-flight artifact leaks.
             */}
            {!isStreaming && inlineArtifacts.length > 0 && (
              <div className="mt-1 space-y-1">
                {inlineArtifacts.map((artifact) =>
                  artifact.source === 'dashboard' ? (
                    <DashboardCard
                      key={artifact.dashboard_id || artifact.id}
                      artifact={artifact}
                      onOpen={() => onArtifactPreview?.(artifact)}
                    />
                  ) : (
                    <InlineArtifactPreview
                      key={artifact.artifact_id || artifact.id}
                      artifact={artifact}
                      onOpen={onArtifactPreview}
                    />
                  )
                )}
              </div>
            )}
            {/* Kimi/GPT-style data-source citations: rendered as small chips
                under the assistant answer when the turn queried datasources.
                Each chip shows the source name + row count; web/file sources
                carrying a `url` render as clickable links (live sources). */}
            {!isUser && !isStreaming && Array.isArray(message.sources) && message.sources.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="inline-flex items-center gap-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">
                  <Database className="h-3 w-3" />
                  {lang === 'en' ? 'Sources' : '数据来源'}
                </span>
                {message.sources.slice(0, 6).map((src, i) => {
                  const srcUrl = typeof src.url === 'string' && /^https?:\/\//.test(src.url) ? src.url : null;
                  const chipInner = (
                    <>
                      {srcUrl
                        ? <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground/70" />
                        : <Table2 className="h-3 w-3 shrink-0" />}
                      <span className="truncate">{src.source_name || src.source_id || 'data source'}</span>
                      {typeof src.rows === 'number' && (
                        <span className="shrink-0 text-muted-foreground/60">{src.rows} rows</span>
                      )}
                    </>
                  );
                  const chipCls = 'inline-flex max-w-[220px] items-center gap-1 rounded-full border border-border bg-secondary/50 px-2 py-0.5 text-[11px] text-muted-foreground';
                  if (srcUrl) {
                    return (
                      <a
                        key={`${src.source_id || src.source_name || ''}-${i}`}
                        href={srcUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        data-testid="source-chip-link"
                        className={`${chipCls} hover:bg-secondary hover:text-foreground`}
                        title={src.source_name || srcUrl}
                      >
                        {chipInner}
                      </a>
                    );
                  }
                  return (
                    <span
                      key={`${src.source_id || src.source_name || ''}-${i}`}
                      className={chipCls}
                      title={src.source_name || src.source_id || ''}
                    >
                      {chipInner}
                    </span>
                  );
                })}
              </div>
            )}
            {/* Message actions: copy, share, like, dislike */}
            {!isStreaming && message.id && (
              <MessageActions
                message={message}
                messageRef={contentRef}
                onFeedback={onFeedback}
                feedbackRating={feedbackRating}
                isStreaming={isStreaming}
                showRoleRating={showRoleRating}
                onRoleRelevance={onRoleRelevance}
                roleRelevanceRating={roleRelevanceRating}
                onRegenerate={onRegenerate}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}