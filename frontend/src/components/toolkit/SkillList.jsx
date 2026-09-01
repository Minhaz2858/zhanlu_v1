import { useState } from 'react';
import { Search, Plus, MoreVertical, Pencil, Trash2 } from 'lucide-react';
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu';

const CATEGORY_ORDER = ['frontend', 'ai', 'automation', 'devops', 'commerce', 'rendering', 'utility', 'general'];

export default function SkillList({ items, icon: Icon, title, description, t, translate, onOpen, onAdd, onEdit, onDelete }) {
  const [query, setQuery] = useState('');

  const filtered = items.filter((x) =>
    !query ||
    (x.name || '').toLowerCase().includes(query.toLowerCase()) ||
    (x.description || '').toLowerCase().includes(query.toLowerCase()) ||
    (x.trigger || '').toLowerCase().includes(query.toLowerCase())
  );

  const grouped = CATEGORY_ORDER
    .map((cat) => ({ cat, list: filtered.filter((x) => (x.category || 'general') === cat) }))
    .filter((g) => g.list.length > 0);

  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-primary" />
          <h2 className="font-display text-lg text-foreground">{title}</h2>
          <span className="text-xs text-muted-foreground">({items.length})</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative w-56">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t.toolkit.searchPlaceholder}
              className="w-full rounded-lg border border-border bg-card pl-9 pr-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary/30"
            />
          </div>
          {onAdd && (
            <button onClick={onAdd} className="inline-flex items-center gap-1 rounded-lg border border-primary bg-primary/5 px-3 py-1.5 text-sm text-primary transition-colors hover:bg-primary/10">
              <Plus className="h-4 w-4" /> {t.toolkit.addSkill}
            </button>
          )}
        </div>
      </div>
      <p className="mb-6 text-xs text-muted-foreground">{description}</p>

      {filtered.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border py-12 text-center text-sm text-muted-foreground">{t.toolkit.empty}</div>
      ) : (
        <div className="space-y-8">
          {grouped.map(({ cat, list }) => (
            <div key={cat}>
              <div className="mb-3 flex items-center gap-2">
                <h3 className="text-sm font-medium text-foreground">{t.toolkit.categories[cat] || cat}</h3>
                <span className="text-xs text-muted-foreground">{list.length}</span>
                <div className="ml-2 h-px flex-1 bg-border" />
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                {list.map((x) => (
                  <div key={x.id} onClick={() => onOpen(x.id)} className="group relative flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm">
                    <div className="mb-2 flex items-center gap-2 pr-8">
                      <h4 className="font-mono text-sm font-medium text-foreground group-hover:text-primary">{x.name}</h4>
                      <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-600">user</span>
                    </div>
                    <p className="mb-4 flex-1 text-xs text-muted-foreground">{x.description ? translate(x.description) : ''}</p>
                    <div className="flex items-center justify-between">
                      <span className="rounded bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700">skills</span>
                      <span className="font-mono text-[10px] text-muted-foreground">/{x.trigger || x.name}</span>
                    </div>
                    {(onEdit || onDelete) && (
                      <div className="absolute right-3 top-3" onClick={(e) => e.stopPropagation()}>
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
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}