import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { MoreVertical, Pencil, Trash2 } from 'lucide-react';
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu';

export default function MySkillsModal({ open, onOpenChange, items, t, onOpen, onEdit, onDelete }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>{t.toolkit.mySkills}</DialogTitle>
        </DialogHeader>
        {items.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border py-16 text-center text-sm text-muted-foreground">{t.toolkit.emptyMy}</div>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((x) => (
              <div key={x.id} onClick={() => { onOpenChange(false); onOpen?.(x.id); }} className="group relative flex cursor-pointer flex-col rounded-xl border border-border bg-card p-4 transition-all hover:border-primary/30 hover:shadow-sm">
                <div className="mb-2 flex items-start justify-between gap-2">
                  <h4 className="line-clamp-1 font-mono text-sm font-medium text-foreground group-hover:text-primary">{x.name}</h4>
                  {(onEdit || onDelete) && (
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
                  )}
                </div>
                <p className="line-clamp-3 flex-1 text-xs leading-relaxed text-muted-foreground">{x.description || ''}</p>
              </div>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}