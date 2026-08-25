/** 2026-08-25: StreamingMarkdown — memoized chunked markdown renderer.
 *
 * Splits content by paragraph boundary at render time. Each completed
 * chunk is memoized (skipped re-render when text is unchanged). Only
 * the LAST (in-progress) chunk re-renders on every token delta.
 *
 * This avoids the O(N²) cost of re-parsing the full markdown on every
 * token, which is what the inline `react-markdown` does today.
 */
import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function splitContentIntoChunks(text) {
  if (!text || typeof text !== 'string') return [];
  // Split on \n\n+ (paragraph breaks) and before headers (#, ##, etc.)
  const parts = text.split(/(\n\n+|(?=^#{1,6}\s))/m);
  return parts.filter((c) => c && c.trim().length > 0);
}

const MemoizedChunk = memo(
  ({ text }) => (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {text}
    </ReactMarkdown>
  ),
  (prev, next) => prev.text === next.text
);

const components = {
  // Default components — can be overridden by parent
};

export default function StreamingMarkdown({ content, isStreaming }) {
  if (!isStreaming) {
    return <MemoizedChunk text={content || ''} />;
  }
  const chunks = splitContentIntoChunks(content || '');
  if (chunks.length === 0) {
    return null;
  }
  return (
    <>
      {chunks.slice(0, -1).map((chunk, i) => (
        <MemoizedChunk key={i} text={chunk} />
      ))}
      <MemoizedChunk key={chunks.length - 1} text={chunks[chunks.length - 1]} />
    </>
  );
}
