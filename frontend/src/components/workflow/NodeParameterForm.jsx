import { useEffect, useRef, useState } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { Button } from '@/components/ui/button';
import { Info, Plus, Trash2 } from 'lucide-react';

/**
 * Schema-driven parameter form — n8n's ParameterInput pattern.
 * Renders the node's `properties` from the shared registry; no per-type
 * switch statements. A property is one of:
 *   string | number | boolean | options | multiOptions | json | notice
 * plus a `collection` renderer for repeatable sub-groups (future).
 *
 * Props:
 *   node        — xyflow node ({ id, type, data: { label, config } })
 *   registry    — { connectionTypes, nodeTypes } from the backend
 *   onChange(nodeId, config, label?) — config patch + optional label change
 */
const inputCls =
  'w-full rounded-lg border border-border bg-card px-2.5 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30';

function Field({ prop, children, required }) {
  return (
    <label className="block">
      <span className="mb-1 flex items-center gap-1 text-xs font-medium text-muted-foreground">
        {prop.displayName}
        {required && <span className="text-red-500">*</span>}
      </span>
      {children}
      {prop.description && <span className="mt-1 block text-[11px] leading-snug text-muted-foreground/80">{prop.description}</span>}
    </label>
  );
}

// Dynamic option loaders (n8n loadOptionsMethod) — DB-backed pickers.
// Each loader receives the node's CURRENT config so dependent pickers
// (e.g. MCP tool after MCP server) can resolve against sibling values.
const OPTION_LOADERS = {
  listAgents: () => base44.entities.AgentApp.list().then((rows) => (Array.isArray(rows) ? rows : [])),
  listTools: () => base44.entities.Tool.list().then((rows) => (Array.isArray(rows) ? rows : [])),
  listKnowledgeBases: () => base44.entities.KnowledgeBase.list().then((rows) => (Array.isArray(rows) ? rows : [])),
  listDatabases: () =>
    base44.entities.KnowledgeBase.list().then((rows) =>
      (Array.isArray(rows) ? rows : []).filter((r) => r.db_type)
    ),
  listMcpServers: () => import('@/api/mcp').then((m) => m.listMcpServers()),
  listMcpTools: (config) =>
    import('@/api/mcp').then((m) =>
      config?.server_id
        ? m.listMcpServerTools(config.server_id).then((res) => res.tools || [])
        : []
    ),
};

function useDynamicOptions(prop, config) {
  const [options, setOptions] = useState(null);
  const loader = prop.loadOptionsMethod ? OPTION_LOADERS[prop.loadOptionsMethod] : null;
  useEffect(() => {
    let alive = true;
    if (loader) {
      setOptions(null);
      Promise.resolve(loader(config))
        .then((rows) => {
          if (!alive) return;
          setOptions(
            (Array.isArray(rows) ? rows : []).map((r) => ({
              name: r.name || r.title || r.tool_name || String(r.id),
              value: String(r.name || r.tool_name || r.id),
            }))
          );
        })
        .catch(() => alive && setOptions([]));
    } else {
      setOptions(prop.options || []);
    }
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prop, JSON.stringify(config || {})]);
  return options;
}

function ExpressionPicker({ upstreamFields, onInsert, onClose }) {
  if (!upstreamFields || upstreamFields.length === 0) return null;
  return (
    <div className="absolute right-0 top-8 z-30 max-h-56 w-64 overflow-y-auto rounded-lg border border-border bg-popover p-1.5 shadow-lg">
      {upstreamFields.map((u) => (
        <div key={u.nodeId} className="mb-1">
          <div className="px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{u.label}</div>
          {u.fields.map((f) => (
            <button
              key={f.path}
              type="button"
              onClick={() => { onInsert(u.nodeId, f.path); onClose(); }}
              className="block w-full rounded px-1.5 py-1 text-left font-mono text-[11px] text-foreground hover:bg-secondary"
            >
              {`{{inputs.${u.nodeId}.${f.path}}}`}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

function StringInput({ prop, value, onChange, upstreamFields }) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const ref = useRef(null);
  const templateable = !!prop.placeholder && prop.placeholder.includes('{{');

  const insert = (nodeId, path) => {
    const el = ref.current;
    const expr = `{{inputs.${nodeId}.${path}}}`;
    const current = typeof value === 'string' ? value : (value ?? '');
    const start = el ? el.selectionStart ?? current.length : current.length;
    const end = el ? el.selectionEnd ?? current.length : current.length;
    const next = current.slice(0, start) + expr + current.slice(end);
    onChange(next);
    requestAnimationFrame(() => {
      if (el) {
        const pos = start + expr.length;
        el.focus();
        el.setSelectionRange(pos, pos);
      }
    });
  };

  const wrap = (field) => (
    <div className="relative">
      {field}
      {templateable && (
        <>
          <button
            type="button"
            title="Insert field"
            onClick={() => setPickerOpen((v) => !v)}
            className="absolute right-1.5 top-1.5 rounded px-1 py-0.5 text-[10px] font-semibold text-muted-foreground hover:bg-secondary hover:text-foreground"
          >
            {'{ }'}
          </button>
          {pickerOpen && <ExpressionPicker upstreamFields={upstreamFields} onInsert={insert} onClose={() => setPickerOpen(false)} />}
        </>
      )}
    </div>
  );

  if (prop.type === 'json') {
    return wrap(
      <textarea
        ref={ref}
        className={`${inputCls} min-h-24 resize-y font-mono text-xs`}
        value={typeof value === 'string' ? value : JSON.stringify(value ?? '', null, 2)}
        onChange={(e) => onChange(e.target.value)}
        placeholder={prop.placeholder}
        spellCheck={false}
      />
    );
  }
  if (prop.type === 'number') {
    return (
      <input
        type="number"
        step="any"
        className={inputCls}
        value={value ?? prop.default ?? 0}
        onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
      />
    );
  }
  return wrap(
    <input
      ref={ref}
      className={`${inputCls} ${templateable ? 'pr-10' : ''}`}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value)}
      placeholder={prop.placeholder}
    />
  );
}

function OptionsInput({ prop, value, onChange, config }) {
  const options = useDynamicOptions(prop, config);
  if (options === null) {
    return <div className="h-8 animate-pulse rounded-lg bg-muted" />;
  }
  if (prop.type === 'multiOptions') {
    const selected = Array.isArray(value) ? value : [];
    return (
      <div className="flex flex-wrap gap-1.5">
        {options.map((o) => {
          const on = selected.includes(o.value);
          return (
            <button
              key={o.value}
              type="button"
              onClick={() => onChange(on ? selected.filter((v) => v !== o.value) : [...selected, o.value])}
              className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
                on ? 'border-primary/60 bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:bg-secondary'
              }`}
            >
              {o.name}
            </button>
          );
        })}
      </div>
    );
  }
  return (
    <select
      className={inputCls}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">— {prop.placeholder || 'select'} —</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.name}</option>
      ))}
    </select>
  );
}

function BooleanInput({ prop, value, onChange }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={!!value}
      onClick={() => onChange(!value)}
      className={`relative h-5 w-9 rounded-full transition-colors ${value ? 'bg-primary' : 'bg-muted'}`}
    >
      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${value ? 'left-4.5 left-[18px]' : 'left-0.5'}`} />
    </button>
  );
}

function CollectionInput({ prop, value, onChange, config }) {
  // Repeatable sub-groups. Each item is an object of the collection's sub-props.
  const items = Array.isArray(value) ? value : [];
  const subProps = prop.options || [];
  return (
    <div className="space-y-2">
      {items.map((item, i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-2">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
              {prop.itemLabel || prop.displayName} {i + 1}
            </span>
            <button type="button" onClick={() => onChange(items.filter((_, j) => j !== i))} className="rounded p-0.5 text-muted-foreground hover:text-destructive">
              <Trash2 className="h-3 w-3" />
            </button>
          </div>
          <div className="space-y-1.5">
            {subProps.map((sp) => (
              <SchemaField
                key={sp.name}
                prop={sp}
                config={config}
                value={item[sp.name]}
                onChange={(v) => {
                  const next = items.map((it, j) => (j === i ? { ...it, [sp.name]: v } : it));
                  onChange(next);
                }}
              />
            ))}
          </div>
        </div>
      ))}
      <Button size="sm" variant="outline" onClick={() => onChange([...items, {}])}>
        <Plus className="mr-1 h-3 w-3" /> {prop.itemLabel || prop.displayName}
      </Button>
    </div>
  );
}

export function SchemaField({ prop, value, onChange, config, upstreamFields }) {
  switch (prop.type) {
    case 'notice':
      return (
        <div className="flex items-start gap-2 rounded-lg border border-info/20 bg-info/5 px-3 py-2 text-[11px] leading-snug text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info" />
          <span>{prop.displayName}</span>
        </div>
      );
    case 'options':
    case 'multiOptions':
      return (
        <Field prop={prop} required={prop.required}>
          <OptionsInput prop={prop} value={value} onChange={onChange} config={config} />
        </Field>
      );
    case 'boolean':
      return (
        <Field prop={prop}>
          <BooleanInput prop={prop} value={value} onChange={onChange} />
        </Field>
      );
    case 'collection':
      return (
        <Field prop={prop}>
          <CollectionInput prop={prop} value={value} onChange={onChange} config={config} />
        </Field>
      );
    default:
      return (
        <Field prop={prop} required={prop.required}>
          <StringInput prop={prop} value={value} onChange={onChange} upstreamFields={upstreamFields} />
        </Field>
      );
  }
}

export default function NodeParameterForm({ node, registry, upstreamFields, onChange }) {
  const { lang } = useLanguage();
  const type = node?.type;
  const def = registry?.nodeTypes?.find((t) => t.name === type);
  const config = node?.data?.config || {};

  if (!def) return null;

  return (
    <div className="space-y-4">
      {def.properties.map((prop) => (
        <SchemaField
          key={prop.name}
          prop={prop}
          config={config}
          upstreamFields={upstreamFields}
          value={config[prop.name]}
          onChange={(v) => onChange({ [prop.name]: v })}
        />
      ))}
      {lang === 'zh' && def.description_zh && (
        <p className="text-[11px] text-muted-foreground/70">{def.description_zh}</p>
      )}
    </div>
  );
}
