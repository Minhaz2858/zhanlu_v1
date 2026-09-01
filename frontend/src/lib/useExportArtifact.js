/**
 * useExportArtifact — client hook for the ReportCard export menu.
 *
 * When the user clicks "Export as DOCX / PPTX / XLSX / PDF / MD" on a
 * ReportCard, we POST to the new ``/api/artifacts/{id}/export/{format}``
 * endpoint (backend ``routers/artifacts.py``).  The endpoint wraps the
 * existing ``run_sandbox_skill`` pipeline: an isolated Docker sandbox
 * builds the file, the result is stored as a fresh version of the
 * artifact (or a sibling artifact), and the PDF preview is generated
 * by headless LibreOffice.
 *
 * On success, the hook resolves with the new artifact's id and URLs;
 * the caller (``ReportCard.jsx``) renders an ``ArtifactPreviewCard``
 * next to the original card so the user gets the download button in
 * the same message bubble.
 */

import { useCallback, useState } from 'react';
import { authFetch } from '@/api/authFetch';

const SUPPORTED_FORMATS = new Set(['docx', 'pptx', 'xlsx', 'pdf', 'html', 'md']);

/**
 * Hook for triggering an export from a ReportCard.
 *
 * @param {object} [opts]
 * @param {(result: ExportResult) => void} [opts.onSuccess] – called with the new artifact info on success.
 * @param {(err: Error) => void} [opts.onError] – called on failure.
 * @returns {object} { exportArtifact, busy, error, busyFormat }
 */
export function useExportArtifact({ onSuccess, onError } = {}) {
  const [busyFormat, setBusyFormat] = useState(null);
  const [error, setError] = useState(null);

  const exportArtifact = useCallback(
    /**
     * @param {object} args
     * @param {string} args.artifactId - source artifact id (the html_report one).
     * @param {string} args.format - 'docx' | 'pptx' | 'xlsx' | 'pdf' | 'html' | 'md'.
     * @param {Array<object>} args.rows - data rows (typically payload.chart.data).
     * @param {string} args.title - report title for the file.
     * @param {string} [args.instructions] - natural-language guidance.
     * @param {string} [args.conversationId]
     * @param {string} [args.agentAppId]
     * @returns {Promise<ExportResult | null>}
     */
    async ({
      artifactId,
      format,
      rows,
      title,
      instructions,
      conversationId,
      agentAppId,
    }) => {
      if (!artifactId) {
        const e = new Error('exportArtifact: artifactId is required');
        setError(e);
        onError?.(e);
        return null;
      }
      const fmt = String(format || '').toLowerCase();
      if (!SUPPORTED_FORMATS.has(fmt)) {
        const e = new Error(
          `exportArtifact: unsupported format ${JSON.stringify(format)}. Supported: ${Array.from(SUPPORTED_FORMATS).join(', ')}`
        );
        setError(e);
        onError?.(e);
        return null;
      }
      if (!Array.isArray(rows) || rows.length === 0) {
        const e = new Error('exportArtifact: rows must be a non-empty array');
        setError(e);
        onError?.(e);
        return null;
      }
      if (!title) {
        const e = new Error('exportArtifact: title is required');
        setError(e);
        onError?.(e);
        return null;
      }

      setBusyFormat(fmt);
      setError(null);
      try {
        const res = await authFetch(
          `/api/artifacts/${encodeURIComponent(artifactId)}/export/${fmt}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              rows,
              title,
              instructions: instructions || '',
              conversation_id: conversationId || null,
              agent_app_id: agentAppId || null,
            }),
          }
        );
        if (!res.ok) {
          const detail = await res.text().catch(() => '');
          throw new Error(
            `Export failed (HTTP ${res.status}): ${detail || res.statusText}`
          );
        }
        /** @type {ExportResult} */
        const result = await res.json();
        onSuccess?.(result);
        return result;
      } catch (e) {
        setError(e);
        onError?.(e);
        return null;
      } finally {
        setBusyFormat(null);
      }
    },
    [onSuccess, onError]
  );

  return { exportArtifact, busy: busyFormat !== null, busyFormat, error };
}

/**
 * @typedef {object} ExportResult
 * @property {string} source_artifact_id
 * @property {string} artifact_id        – the new file artifact (DOCX/PPTX/…)
 * @property {string} format
 * @property {string} title
 * @property {string} preview_url
 * @property {string} download_url
 * @property {string} job_id
 * @property {Array<object>} files
 */

export default useExportArtifact;
