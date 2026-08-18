/**
 * Detect an explicit file-format request from a user message.
 *
 * Returns the canonical extension (`'docx' | 'pptx' | 'pdf' | 'xlsx' | 'md'`)
 * when the user clearly asked for a file in that format, or `null` when
 * no file-format intent is detected.
 *
 * This is used by the chat UI to enforce strict format matching: when
 * the user asks for a DOCX, only DOCX (or compatible) artifacts are
 * previewed. Other artifact types (e.g. an HTML report card produced
 * by mistake) trigger a "format mismatch" warning instead of being
 * rendered as the requested file type.
 */
const FILE_FORMAT_PATTERNS = [
  { ext: 'docx', regex: /\b(docx|word\s*document|word\s*file|ms\s*word|\.docx)\b/i },
  { ext: 'pptx', regex: /(?<![a-zA-Z])(pptx|ppt|powerpoint)(?![a-zA-Z])|\.pptx?\b|presentation\s*deck|slide\s*deck|slides?\s*deck|演示文稿|幻灯片/i },
  { ext: 'pdf',  regex: /\b(\.pdf|adobe\s*pdf)\b/i },
  { ext: 'xlsx', regex: /\b(xlsx|excel\s*spreadsheet|excel\s*file|spreadsheet|\.xlsx)\b/i },
  { ext: 'md',   regex: /\b(markdown|\.md|md\s*file)\b/i },
];

export function detectFileFormatRequest(text) {
  if (!text || typeof text !== 'string') return null;
  for (const { ext, regex } of FILE_FORMAT_PATTERNS) {
    if (regex.test(text)) return ext;
  }
  // Low-specificity fallback: bare "pdf" word
  if (/\bpdf\b/i.test(text)) return 'pdf';
  return null;
}
