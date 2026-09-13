import { useEffect, useState } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import { getNodeTypeRegistry, nodeTypeOf } from '@/lib/nodeTypeRegistry';
import { nodeLabel } from '@/lib/workflowGraph';
import NodeParameterForm from '@/components/workflow/NodeParameterForm';
import { X } from 'lucide-react';

/**
 * NDV-style settings drawer — n8n's Parameters tab.
 * The form itself is auto-generated from the shared node-type registry
 * (NodeParameterForm); this drawer adds the node header (icon, type name,
 * editable label) and the empty-selection state.
 */
export default function NodeSettingsDrawer({ node, registry, onClose, onConfigChange }) {
  const { lang } = useLanguage();
  const [localRegistry, setLocalRegistry] = useState(registry || null);
  const type = node?.type;
  const def = localRegistry ? nodeTypeOf(type) : null;

  useEffect(() => {
    if (!localRegistry) {
      getNodeTypeRegistry().then(setLocalRegistry).catch(() => {});
    }
  }, [localRegistry]);

  if (!node || !def) {
    return (
      <div className="flex h-full w-80 flex-col border-l border-border bg-card/60">
        <div className="flex items-center justify-between px-4 py-3">
          <span className="text-sm font-semibold text-foreground">{lang === 'zh' ? '节点设置' : 'Node Settings'}</span>
          <button onClick={onClose} className="rounded-md p-1 text-muted-foreground hover:bg-secondary"><X className="h-4 w-4" /></button>
        </div>
        <div className="px-4 py-8 text-center text-xs text-muted-foreground">
          {lang === 'zh' ? '选择一个节点以编辑其设置' : 'Select a node to edit its settings'}
        </div>
      </div>
    );
  }

  const config = node.data?.config || {};

  return (
    <div className="flex h-full w-80 flex-col border-l border-border bg-card/60">
      {/* Header: node identity + editable label */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-base leading-none" style={{ color: def.color }}>{def.icon}</span>
          <div className="min-w-0">
            <div className="truncate text-xs font-semibold text-foreground">{nodeTypeLabel(def, type, lang)}</div>
            <input
              value={node.data.label || ''}
              onChange={(e) => onConfigChange(node.id, config, e.target.value)}
              className="w-full truncate rounded border border-transparent bg-transparent px-1 py-0.5 text-[11px] text-muted-foreground focus:border-border focus:bg-card focus:outline-none"
              placeholder={nodeTypeLabel(def, type, lang)}
            />
          </div>
        </div>
        <button onClick={onClose} className="rounded-md p-1 text-muted-foreground hover:bg-secondary"><X className="h-4 w-4" /></button>
      </div>

      {/* Parameters tab body (schema-driven) */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {localRegistry ? (
          <NodeParameterForm
            node={node}
            registry={localRegistry}
            onChange={(patch) => onConfigChange(node.id, { ...config, ...patch })}
          />
        ) : (
          <div className="h-24 animate-pulse rounded-lg bg-muted" />
        )}
      </div>
    </div>
  );
}

function nodeTypeLabel(def, type, lang) {
  return nodeLabel(type, lang) || def.displayName || def.name;
}
