import { useEffect, useState } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import { getNodeTypeRegistry } from '@/lib/nodeTypeRegistry';

/**
 * Node palette — n8n-style node picker, schema-driven.
 * Groups nodes by category from the shared registry; items are draggable
 * onto the canvas (dataTransfer 'application/zhanlu-node' = type name).
 */
const CATEGORY_ORDER = ['Triggers', 'AI', 'Logic', 'Data', 'Output'];

// Power-user nodes stay fully functional in existing flows (and the backend
// still runs them) but are NOT offered in the palette — keep the picker
// simple: only show nodes people understand.
const HIDDEN_PALETTE_TYPES = new Set(['code']);

export default function NodePalette({ collapsed, onToggle, registry }) {
  const { lang } = useLanguage();
  const [localRegistry, setLocalRegistry] = useState(registry || null);

  useEffect(() => {
    if (!localRegistry) {
      getNodeTypeRegistry().then(setLocalRegistry).catch(() => {});
    }
  }, [localRegistry]);

  const types = (localRegistry?.nodeTypes || []).filter((t) => !HIDDEN_PALETTE_TYPES.has(t.name));
  const categories = CATEGORY_ORDER.filter((c) => types.some((t) => t.category === c));
  const extra = [...new Set(types.map((t) => t.category))].filter((c) => !CATEGORY_ORDER.includes(c));
  const allCategories = [...categories, ...extra];

  function startDrag(event, typeName) {
    event.dataTransfer.setData('application/zhanlu-node', typeName);
    event.dataTransfer.effectAllowed = 'move';
  }

  return (
    <div className={`flex h-full flex-col border-r border-border bg-card/60 ${collapsed ? 'w-12' : 'w-56'}`}>
      <div className="flex items-center justify-between px-3 py-2.5">
        {!collapsed && (
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {lang === 'zh' ? '节点库' : 'Nodes'}
          </span>
        )}
        <button onClick={onToggle} className="rounded-md p-1 text-muted-foreground hover:bg-secondary" title={collapsed ? (lang === 'zh' ? '展开' : 'Expand') : (lang === 'zh' ? '收起' : 'Collapse')}>
          {collapsed ? '»' : '«'}
        </button>
      </div>

      {!collapsed && (
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-3 pb-4">
          {allCategories.map((cat) => (
            <div key={cat}>
              <div className="mb-1.5 flex items-center gap-1.5">
                <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {lang === 'zh' ? catLabelZh(cat) : cat}
                </span>
              </div>
              <div className="space-y-1.5">
                {types.filter((t) => t.category === cat).map((def) => (
                  <div
                    key={def.name}
                    draggable
                    onDragStart={(e) => startDrag(e, def.name)}
                    title={lang === 'zh' && def.description_zh ? def.description_zh : def.description}
                    className="flex cursor-grab items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-2 text-xs transition-colors hover:border-primary/50 hover:bg-secondary active:cursor-grabbing"
                  >
                    <span style={{ color: def.color }}>{def.icon}</span>
                    <span className="truncate font-medium text-foreground">{def.displayName}</span>
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

function catLabelZh(cat) {
  return { Triggers: '触发器', AI: 'AI', Logic: '逻辑', Data: '数据', Output: '输出' }[cat] || cat;
}
