import { memo, useContext } from 'react';
import { Handle, Position } from '@xyflow/react';
import { nodeConfigError, nodeTypeDef, nodeLabel } from '@/lib/workflowGraph';
import { connectionMeta, nodeTypeOf, useNodeRegistry } from '@/lib/nodeTypeRegistry';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * n8n-style canvas node: ONE generic renderer driven by the shared
 * node-type registry (no per-type components).
 *
 * - Typed connection ports: one Handle per input/output port declared in
 *   the registry. Main ports use the node color; supplemental ports
 *   (ai_llm / ai_tool / ai_kb) use the connection-type color and render
 *   with a small type label chip.
 * - Body summary is derived generically from the first notable configured
 *   property values (schema-driven, like n8n's default render).
 *
 * Handle id convention: `${type}-${connectionType}` for the FIRST port of a
 * connection type, `${type}-${connectionType}-${i}` for repeats (e.g.
 * Switch's Case 1/2/3 + Default = `source-main`, `source-main-1`, ...).
 * Edge records carry sourceHandle/targetHandle so connection types and port
 * indices survive round-trips through the graph JSON.
 */

function portId(kind, port, index, ports) {
  const type = typeof port === 'string' ? port : port.type;
  // First port of this type keeps the bare id (backward compat with old
  // edges); repeats get a numeric index suffix.
  const first = ports.findIndex((p) => (typeof p === 'string' ? p : p.type) === type) === index;
  return first ? `${kind}-${type}` : `${kind}-${type}-${index}`;
}

function portLabel(port) {
  return typeof port === 'string' ? null : port.label || null;
}

function PortHandles({ kind, ports, nodeColor, registry }) {
  const pos = kind === 'target' ? Position.Left : Position.Right;
  const step = 100 / Math.max(ports.length, 1);
  return (
    <>
      {ports.map((port, i) => {
        const type = typeof port === 'string' ? port : port.type;
        const supplemental = type !== 'main';
        const meta = registry ? connectionMeta(type) : { color: nodeColor };
        const color = supplemental ? meta.color : nodeColor;
        const label = portLabel(port) || meta.label;
        const id = portId(kind, port, i, ports);
        return (
          <div key={id} className="absolute" style={{ top: `${step * (i + 0.5)}%`, left: kind === 'target' ? 0 : undefined, right: kind === 'source' ? 0 : undefined, zIndex: 10 }}>
            <Handle
              id={id}
              type={kind}
              position={pos}
              className={supplemental ? '!h-2 !w-2 !border-2 !bg-card' : '!h-2.5 !w-2.5 !border-2 !bg-card'}
              style={{ borderColor: color, backgroundColor: supplemental ? '#fff' : color }}
            />
            {(supplemental || label) && (
              <span
                className="pointer-events-none absolute top-1/2 -translate-y-1/2 whitespace-nowrap rounded px-1 py-px text-[8px] font-semibold"
                style={{ color, backgroundColor: supplemental ? `${color}1a` : 'transparent', left: kind === 'source' ? undefined : undefined, right: kind === 'source' ? 'auto' : undefined, marginLeft: kind === 'source' ? 8 : 0, marginRight: kind === 'target' ? 8 : 0 }}
              >
                {label}
              </span>
            )}
          </div>
        );
      })}
    </>
  );
}

function NodeShell({ type, config, selected, registry, children }) {
  const { lang } = useLanguage();
  const def = nodeTypeOf(type) || nodeTypeDef(type);
  const color = def?.color || '#8b5cf6';
  const error = nodeConfigError(type, config);
  const inputs = def?.inputs || [];
  const outputs = def?.outputs || [];
  return (
    <div
      className={`w-56 rounded-xl border bg-card shadow-sm transition-shadow ${
        selected ? 'shadow-lg ring-2' : 'hover:shadow'
      }`}
      style={{
        borderColor: error ? '#ef4444' : color,
        ['--tw-ring-color']: error ? '#ef4444' : color,
      }}
    >
      {/* Header */}
      <div
        className="flex items-center gap-2 rounded-t-xl px-3 py-2"
        style={{ backgroundColor: `${color}18`, borderBottom: `1px solid ${color}40` }}
      >
        <span className="text-sm" style={{ color }}>{def?.icon || '•'}</span>
        <span className="min-w-0 flex-1 truncate text-xs font-semibold text-foreground">
          {nodeLabel(type, lang) || def?.displayName || type}
        </span>
        {selected && <span className="text-[9px] uppercase tracking-wide text-muted-foreground">{lang === 'zh' ? '已选' : 'sel'}</span>}
      </div>
      {/* Body */}
      <div className="px-3 py-2 text-[11px] text-muted-foreground">
        {children}
        {error && <div className="mt-1.5 text-[10px] font-medium text-red-500">{error}</div>}
      </div>
      {/* Typed connection ports */}
      <PortHandles kind="target" ports={inputs} nodeColor={color} registry={registry} />
      <PortHandles kind="source" ports={outputs} nodeColor={color} registry={registry} />
    </div>
  );
}

/** Generic summary: first notable configured values, schema-driven. */
function NodeSummary({ def, config }) {
  if (!def) return null;
  const notable = (def.properties || [])
    .filter((p) => !['notice', 'boolean', 'json'].includes(p.type))
    .filter((p) => {
      const v = config?.[p.name];
      return v !== undefined && v !== null && v !== '' && v !== p.default;
    })
    .slice(0, 2);
  if (notable.length === 0) {
    return <div className="line-clamp-2 break-words opacity-70">{def.description || def.displayName}</div>;
  }
  return (
    <div className="space-y-1">
      {notable.map((p) => {
        const raw = config[p.name];
        const display = p.type === 'collection' && Array.isArray(raw)
          ? `${raw.length} ${raw.length === 1 ? 'item' : 'items'}`
          : String(raw).slice(0, 60);
        return (
          <div key={p.name} className="line-clamp-1 break-words">
            <span className="opacity-60">{p.displayName}: </span>
            <span className="text-foreground/80">{display}</span>
          </div>
        );
      })}
    </div>
  );
}

// One generic component for every node type; the registry comes from the
// NodeRegistryContext (module cache, populated on first canvas mount).
// NOTE: xyflow passes the node's type as a PROP (`type`), not inside data.
const GenericNode = memo(({ data, selected, type }) => {
  const registry = useNodeRegistry();
  const def = nodeTypeOf(type) || nodeTypeDef(type);
  return (
    <NodeShell type={type} config={data.config} selected={selected} registry={registry}>
      <NodeSummary def={def} config={data.config} />
    </NodeShell>
  );
});

// Explicit per-type entries so React Flow's nodeTypes map is stable, but each
// renders the SAME generic component (n8n uses render-type + registry).
export const workflowNodeTypes = Object.fromEntries(
  ['trigger_chat', 'trigger_manual', 'trigger_schedule', 'trigger_webhook',
   'llm', 'agent', 'tool', 'kb_search', 'db_query', 'mcp_tool',
   'http_request', 'code', 'set_fields', 'split_out', 'remove_duplicates',
   'condition', 'switch', 'output'].map((t) => [t, GenericNode])
);
