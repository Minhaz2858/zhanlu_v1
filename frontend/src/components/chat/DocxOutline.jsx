/**
 * DocxOutline — collapsible sidebar of DOCX headings.
 *
 * Pure presentational component.  Receives the heading list (extracted
 * server-side by `extract_docx_outline`) and an onJump callback.  The
 * parent is responsible for actually scrolling to the anchor — we just
 * fire the id.
 */
import { List } from 'lucide-react';
import { cn } from '@/lib/utils';

export default function DocxOutline({ outline = [], onJump, className }) {
  if (!Array.isArray(outline) || outline.length === 0) return null;

  return (
    <nav
      aria-label="Document outline"
      className={cn(
        'flex flex-col gap-0.5 border-r border-border/60 bg-card/40 py-3 pr-2',
        'overflow-y-auto text-xs',
        className,
      )}
    >
      <div className="mb-1 flex items-center gap-1.5 px-3 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        <List className="h-3 w-3" />
        Outline
      </div>
      {outline.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={() => onJump?.(item.id)}
          className={cn(
            'rounded-md px-3 py-1 text-left text-foreground/80 transition-colors',
            'hover:bg-secondary/70 hover:text-foreground',
            // indent per level (capped at 3)
            item.level === 1 && 'pl-3',
            item.level === 2 && 'pl-6',
            item.level >= 3 && 'pl-9',
          )}
        >
          {item.text}
        </button>
      ))}
    </nav>
  );
}
