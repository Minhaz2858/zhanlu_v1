/**
 * ArtifactPreviewCard — Claude-style inline preview card for governed artifacts.
 *
 * Renders inside assistant bubbles with:
 * - 48px file-type hero icon with tinted background
 * - Header row: title + version · size + status badge
 * - Inline Markdown/code preview (first 20 lines) for .md artifacts
 * - Primary "Open" CTA → ArtifactPreviewSheet
 * - Secondary "Download" CTA
 * - Smooth expand/collapse with shadow lift on hover
 * - 200ms max-height transition for the inline preview area
 */

import { useState, useEffect, useCallback } from 'react';
import { authFetch } from '@/api/authFetch';
import {
  FileText, Presentation, FileSpreadsheet, FileCode, Image,
  Download, Eye, RefreshCw, CheckCircle2, Clock, AlertCircle,
  Loader2, ChevronDown, ChevronUp, ExternalLink, Copy, Check,
} from 'lucide-react';
import DocxArtifactPreview from './DocxArtifactPreview';
import PptxArtifactPreview from './PptxArtifactPreview';
import HtmlReportArtifactPreview from './HtmlReportArtifactPreview';

const API_BASE = '/api';

// ── Type metadata ────────────────────────────────────────────────
const TYPE_META = {
  pptx: { icon: Presentation, color: 'text-orange-500', bg: 'bg-orange-50', label: 'PPT', borderColor: 'border-orange-200' },
  docx: { icon: FileText, color: 'text-blue-500', bg: 'bg-blue-50', label: 'DOC', borderColor: 'border-blue-200' },
  pdf: { icon: FileText, color: 'text-red-500', bg: 'bg-red-50', label: 'PDF', borderColor: 'border-red-200' },
  md: { icon: FileCode, color: 'text-gray-600', bg: 'bg-gray-50', label: 'MD', borderColor: 'border-gray-200' },
  html: { icon: FileCode, color: 'text-purple-500', bg: 'bg-purple-50', label: 'HTML', borderColor: 'border-purple-200' },
  xlsx: { icon: FileSpreadsheet, color: 'text-green-500', bg: 'bg-green-50', label: 'XLS', borderColor: 'border-green-200' },
  chart: { icon: FileText, color: 'text-cyan-500', bg: 'bg-cyan-50', label: 'Chart', borderColor: 'border-cyan-200' },
  dashboard: { icon: FileText, color: 'text-indigo-500', bg: 'bg-indigo-50', label: 'Dashboard', borderColor: 'border-indigo-200' },
  image: { icon: Image, color: 'text-pink-500', bg: 'bg-pink-50', label: 'Image', borderColor: 'border-pink-200' },
  mini_app: { icon: FileCode, color: 'text-teal-500', bg: 'bg-teal-50', label: 'App', borderColor: 'border-teal-200' },
  html_report: { icon: FileCode, color: 'text-purple-500', bg: 'bg-purple-50', label: 'Report', borderColor: 'border-purple-200' },
};

const STATUS_META = {
  draft: { icon: Clock, color: 'text-gray-500', bg: 'bg-gray-100', label: 'Draft' },
  building: { icon: Loader2, color: 'text-blue-500', bg: 'bg-blue-100', label: 'Building…', spin: true },
  preview_ready: { icon: Eye, color: 'text-cyan-500', bg: 'bg-cyan-100', label: 'Preview Ready' },
  editing: { icon: RefreshCw, color: 'text-amber-500', bg: 'bg-amber-100', label: 'Editing' },
  validated: { icon: CheckCircle2, color: 'text-green-500', bg: 'bg-green-100', label: 'Validated' },
  approved: { icon: CheckCircle2, color: 'text-green-600', bg: 'bg-green-100', label: 'Approved' },
  published: { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-100', label: 'Published' },
  failed: { icon: AlertCircle, color: 'text-red-500', bg: 'bg-red-100', label: 'Failed' },
  archived: { icon: Clock, color: 'text-gray-400', bg: 'bg-gray-100', label: 'Archived' },
};

// ── Helpers ───────────────────────────────────────────────────────
function formatSize(bytes) {
  if (!bytes) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function fetchJSON(url) {
  const res = await authFetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/**
 * Fetch the raw preview text for a Markdown artifact.
 * Returns the first `maxLines` lines as a string.
 */
async function fetchMDPreview(artifactId, maxLines = 20) {
  try {
    const res = await authFetch(`${API_BASE}/artifacts/${artifactId}/preview`);
    if (!res.ok) return null;
    const text = await res.text();
    const lines = text.split('\n').slice(0, maxLines);
    return lines.join('\n');
  } catch {
    return null;
  }
}

// ── Inline Markdown Preview ───────────────────────────────────────
function MarkdownInlinePreview({ artifactId }) {
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    fetchMDPreview(artifactId).then((text) => {
      if (active) { setPreview(text); setLoading(false); }
    });
    return () => { active = false; };
  }, [artifactId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-4 text-xs text-muted-foreground">
        <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />
        Loading preview…
      </div>
    );
  }

  if (!preview) {
    return (
      <div className="py-3 text-center text-xs text-muted-foreground">
        No content preview available.
      </div>
    );
  }

  return (
    <pre className="max-h-[320px] overflow-y-auto bg-secondary/50 p-3 font-mono text-[11px] leading-relaxed text-foreground/80 whitespace-pre-wrap select-text">
      {preview}
    </pre>
  );
}

// ── Action Buttons ────────────────────────────────────────────────
function ActionButtons({ hasPreview, expanded, onToggleExpand, onOpen, downloadUrl, title }) {
  const [copied, setCopied] = useState(false);

  function handleCopyLink() {
    navigator.clipboard.writeText(`${window.location.origin}${downloadUrl}`).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  }

  return (
    <div className="flex flex-wrap items-center gap-2 px-4 py-2.5">
      {/* Primary: Open in Sheet */}
      <button
        onClick={onOpen}
        className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground shadow-sm transition-colors hover:bg-primary/90 active:scale-[0.98]"
      >
        <ExternalLink className="h-3.5 w-3.5" /> Open
      </button>

      {/* Secondary: Download */}
      <a
        href={downloadUrl}
        download
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
      >
        <Download className="h-3.5 w-3.5" /> Download
      </a>

      {/* Inline Preview Toggle */}
      {hasPreview && (
        <button
          onClick={onToggleExpand}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
        >
          {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
          {expanded ? 'Collapse' : 'Preview'}
        </button>
      )}

      {/* Copy Link (ghost) */}
      <div className="ml-auto">
        <button
          onClick={handleCopyLink}
          className="inline-flex items-center gap-1 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          title="Copy download link"
        >
          {copied ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className="h-3.5 w-3.5" />}
        </button>
      </div>
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────
export default function ArtifactPreviewCard({
  artifactId,
  artifact: initialArtifact,
  onOpen,            // (artifactSummary) => void — opens ArtifactPreviewSheet
}) {
  const [artifact, setArtifact] = useState(initialArtifact || null);
  const [loading, setLoading] = useState(!initialArtifact && !!artifactId);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(false);

  const loadArtifact = useCallback(async () => {
    if (!artifactId) return;
    try {
      setLoading(true);
      const data = await fetchJSON(`${API_BASE}/artifacts/${artifactId}`);
      setArtifact(data);
      setError(null);
    } catch (e) {
      console.error('ArtifactPreviewCard: fetch failed', e);
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [artifactId]);

  // Auto-retry on 404: when the assistant just finished creating an
  // artifact, the backend may still be in the middle of the commit
  // transaction.  A short retry (1s, then 2s) handles the race without
  // requiring the user to click anything.
  useEffect(() => {
    if (!error || !artifactId) return;
    const is404 = /HTTP 404|not found/i.test(error);
    if (!is404) return;
    const timers = [
      setTimeout(() => { setError(null); loadArtifact(); }, 1000),
      setTimeout(() => { setError(null); loadArtifact(); }, 3000),
    ];
    return () => timers.forEach((t) => clearTimeout(t));
  }, [error, artifactId, loadArtifact]);

  useEffect(() => {
    if (!initialArtifact && artifactId) loadArtifact();
  }, [artifactId, initialArtifact, loadArtifact]);

  // ── Loading State ─────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-border bg-card p-4 text-sm text-muted-foreground shadow-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading artifact…
      </div>
    );
  }

  // ── Error State ───────────────────────────────────────────────
  if (error || !artifact) {
    const is404 = /HTTP 404|not found/i.test(error || '');
    return (
      <div className="flex flex-col gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive shadow-sm">
        <div className="flex items-center gap-2">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>Failed to load artifact: {error || 'Unknown error'}</span>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <button
            onClick={() => { setError(null); loadArtifact(); }}
            className="inline-flex items-center gap-1 rounded-md border border-destructive/30 bg-background px-2.5 py-1 text-destructive hover:bg-destructive/10"
          >
            <RefreshCw className="h-3 w-3" /> Retry
          </button>
          {is404 && artifactId && (
            <a
              href={`${API_BASE}/artifacts/${artifactId}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1 text-foreground hover:bg-secondary"
            >
              <ExternalLink className="h-3 w-3" /> Open in new tab
            </a>
          )}
          {is404 && (
            <span className="text-muted-foreground">
              The artifact may still be finalizing. Auto-retrying…
            </span>
          )}
        </div>
      </div>
    );
  }

  // ── Derived Values ─────────────────────────────────────────────
  const typeMeta = TYPE_META[artifact.artifact_type] || TYPE_META.docx;
  const statusMeta = STATUS_META[artifact.status] || STATUS_META.draft;
  const TypeIcon = typeMeta.icon;
  const StatusIcon = statusMeta.icon;
  const hasPreview = artifact.status === 'preview_ready' || artifact.status === 'validated' ||
                     artifact.status === 'approved' || artifact.status === 'published';

  const previewUrl = `${API_BASE}/artifacts/${artifact.id}/preview`;
  const downloadUrl = `${API_BASE}/artifacts/${artifact.id}/download`;

  const isImage = artifact.artifact_type === 'image';
  const isMD = artifact.artifact_type === 'md';
  const isHTML = artifact.artifact_type === 'html';
  const isDOCX = artifact.artifact_type === 'docx';
  const isPPTX = artifact.artifact_type === 'pptx';
  const isHTMLReport = artifact.artifact_type === 'html_report';
  // PPTX is now rendered by its own inline reader (PptxArtifactPreview),
  // so it no longer falls into the iframe-PDF branch.
  const isPDF = artifact.artifact_type === 'pdf' || artifact.artifact_type === 'xlsx';

  // Latest version number
  const latestVersion = artifact.versions?.length > 0
    ? artifact.versions[0].version_number
    : 1;

  const displaySize = formatSize(artifact.file_size);

  // Build the summary object expected by ArtifactPreviewSheet
  const buildSheetSummary = () => ({
    id: artifact.id,
    type: artifact.artifact_type,
    title: artifact.title,
    file_name: `${artifact.title}.${artifact.artifact_type === 'md' ? 'md' : artifact.artifact_type}`,
    preview_url: previewUrl,
    file_url: downloadUrl,
    has_preview: hasPreview,
    file_size: artifact.file_size || null,
    preview_outline: artifact.preview_outline || [],
    ms_word_open_url: artifact.ms_word_open_url || null,
  });

  function handleToggleExpand() {
    setExpanded((prev) => !prev);
  }

  function handleOpen() {
    if (onOpen) {
      onOpen(buildSheetSummary());
    }
  }

  // ── Render ────────────────────────────────────────────────────
  return (
    <div className="group overflow-hidden rounded-xl border border-border bg-card shadow-sm transition-shadow duration-200 hover:shadow-md">
      {/* Header — 48px hero icon */}
      <div className="flex items-center gap-3 border-b border-border px-4 py-3">
        {/* 48px tinted square icon */}
        <div className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-xl ${typeMeta.bg} ring-1 ring-inset ${typeMeta.borderColor || 'ring-gray-200'}`}>
          <TypeIcon className={`h-6 w-6 ${typeMeta.color}`} />
        </div>

        {/* Title + meta row */}
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-foreground">
            {artifact.title}
          </p>
          <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="font-medium">{typeMeta.label}</span>
            <span className="opacity-40">·</span>
            <span>v{latestVersion}</span>
            {displaySize && (
              <>
                <span className="opacity-40">·</span>
                <span>{displaySize}</span>
              </>
            )}
          </div>
        </div>

        {/* Status badge — top right */}
        <span className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${statusMeta.bg} ${statusMeta.color}`}>
          <StatusIcon className={`h-3 w-3 ${statusMeta.spin ? 'animate-spin' : ''}`} />
          {statusMeta.label}
        </span>
      </div>

      {/* Expandable Preview Area */}
      <div
        className={`overflow-hidden transition-all duration-300 ease-in-out ${
          expanded ? 'max-h-[600px] opacity-100' : 'max-h-0 opacity-0'
        }`}
      >
        {/* MD: Inline code preview block */}
        {isMD && expanded && (
          <div className="border-b border-border">
            <MarkdownInlinePreview artifactId={artifact.id} />
          </div>
        )}

        {/* Image preview */}
        {isImage && expanded && (
          <div className="border-b border-border bg-secondary/10">
            <img src={previewUrl} alt={artifact.title} className="mx-auto max-h-[400px] object-contain p-4" />
          </div>
        )}

        {/* HTML iframe preview */}
        {isHTML && expanded && (
          <div className="border-b border-border">
            <iframe
              src={previewUrl}
              title={artifact.title}
              className="h-[400px] w-full border-0"
              sandbox="allow-same-origin"
            />
          </div>
        )}

        {/* DOCX: inline reader (mammoth → HTML) */}
        {isDOCX && expanded && (
          <div className="border-b border-border bg-background">
            <div className="max-h-[480px] overflow-hidden">
              <DocxArtifactPreview
                artifactId={artifact.id}
                outline={artifact.preview_outline || []}
                title={artifact.title}
                downloadUrl={downloadUrl}
              />
            </div>
          </div>
        )}

        {/* PPTX: inline reader (python-pptx → sanitized HTML) */}
        {isPPTX && expanded && (
          <div className="border-b border-border bg-background">
            <div className="max-h-[480px] overflow-hidden">
              <PptxArtifactPreview
                artifactId={artifact.id}
                outline={artifact.preview_outline || []}
                title={artifact.title}
                downloadUrl={downloadUrl}
              />
            </div>
          </div>
        )}

        {/* HTML Report: outline sidebar + full preview */}
        {isHTMLReport && expanded && (
          <div className="border-b border-border bg-background">
            <div className="h-[500px]">
              <HtmlReportArtifactPreview
                artifactId={artifact.id}
                title={artifact.title}
                downloadUrl={downloadUrl}
              />
            </div>
          </div>
        )}

        {/* PDF / Office iframe preview */}
        {isPDF && expanded && (
          <div className="border-b border-border">
            <iframe
              src={previewUrl}
              title={artifact.title}
              className="h-[400px] w-full border-0"
            />
          </div>
        )}
      </div>

      {/* Action Buttons — primary Open, secondary Download, Preview toggle, Copy */}
      <ActionButtons
        hasPreview={hasPreview}
        expanded={expanded}
        onToggleExpand={handleToggleExpand}
        onOpen={handleOpen}
        downloadUrl={downloadUrl}
        title={artifact.title}
      />
    </div>
  );
}

/**
 * ArtifactPreviewCardList — Renders multiple artifact cards for a message.
 *
 * Fetches artifacts linked to a message from /api/messages/{messageId}/artifacts
 * and renders an ArtifactPreviewCard for each.
 */
export function ArtifactPreviewCardList({ messageId, onArtifactPreview }) {
  const [artifacts, setArtifacts] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!messageId) { setLoading(false); return; }
    let active = true;
    fetchJSON(`${API_BASE}/messages/${messageId}/artifacts`)
      .then((data) => { if (active) setArtifacts(data); })
      .catch(() => { /* silent fail — no artifacts for this message */ })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [messageId]);

  if (loading || artifacts.length === 0) return null;

  return (
    <div className="space-y-2.5 mt-2">
      {artifacts.map((a, i) => (
        <ArtifactPreviewCard
          key={a.artifact_id || i}
          artifact={a}
          artifactId={a.artifact_id}
          onOpen={onArtifactPreview}
        />
      ))}
    </div>
  );
}
