import { MoreVertical, Pencil, Trash2, Plus, Loader2, Check } from 'lucide-react';
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu';

export default function SkillCardGrid({ items, t, onOpen, onEdit, onDelete, onAdd, addedNames, addingNames, isMySkills }) {
  if (items.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border py-16 text-center text-sm text-muted-foreground">
        {isMySkills ? t.toolkit.emptyMy : t.toolkit.empty}
      </div>
    );
  }
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {items.map((x) => {
        const key = (x.name || '').toLowerCase();
        const isAdded = addedNames?.has(key);
        const isAdding = addingNames?.has(key);
        return (
        <div key={x.id} onClick={() => onOpen?.(x.id)} className="group relative flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-all hover:border-primary/30 hover:shadow-sm">
          <div className="mb-2 flex items-start justify-between gap-2">
            <h4 className="line-clamp-1 font-mono text-sm font-medium text-foreground group-hover:text-primary">{x.name}</h4>
            {isMySkills ? (
              (onEdit || onDelete) && (
                <div onClick={(e) => e.stopPropagation()} className="shrink-0">
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <button className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground">
                        <MoreVertical className="h-4 w-4" />
                      </button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      {onEdit && <DropdownMenuItem onClick={() => onEdit(x)}><Pencil className="mr-2 h-3.5 w-3.5" /> {t.toolkit.edit}</DropdownMenuItem>}
                      {onDelete && <DropdownMenuItem onClick={() => onDelete(x)} className="text-destructive"><Trash2 className="mr-2 h-3.5 w-3.5" /> {t.toolkit.delete}</DropdownMenuItem>}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              )
            ) : onAdd ? (
              isAdded ? (
                <div
                  title={t.toolkit.addedToMySkills || 'Added to My Skills'}
                  aria-label="added"
                  className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-emerald-200 bg-emerald-50 text-emerald-600"
                >
                  <Check className="h-4 w-4" />
                </div>
              ) : (
                <button
                  onClick={(e) => { e.stopPropagation(); onAdd(x); }}
                  disabled={isAdding}
                  title={t.toolkit.addToMySkills}
                  className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:border-primary hover:bg-primary/5 hover:text-primary disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-border disabled:hover:bg-transparent disabled:hover:text-muted-foreground"
                >
                  {isAdding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                </button>
              )
            ) : null}
          </div>
          <p className="mb-4 line-clamp-3 flex-1 text-xs leading-relaxed text-muted-foreground">{x.description || ''}</p>
          <div className="flex items-center justify-between border-t border-border pt-3">
            <span className="text-[11px] text-muted-foreground">{(x.call_count || 0).toLocaleString()} {t.toolkit.uses}</span>
            {/* Publisher tag is hidden for the bundled marketplace skills
                (``publisher === "hermes"`` is the default the sync writes
                when the SKILL.md frontmatter has no ``author:`` field) and
                for any empty value — showing the same tag on every card is
                visual noise. Real publisher names (e.g. "Acme Corp" on a
                user-uploaded skill) still display. */}
            {(() => {
              const pub = (x.publisher || '').trim();
              if (!pub) return null;
              if (pub.toLowerCase() === 'hermes') return null;
              return (
                <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                  {isMySkills ? t.toolkit.me : pub}
                </span>
              );
            })()}
          </div>
        </div>
        );
      })}
    </div>
  );
}