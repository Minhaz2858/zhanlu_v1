import { useState } from 'react';
import { Checkbox } from '@/components/ui/checkbox';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Database, Table2, Columns3 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useLanguage } from '@/lib/LanguageProvider';

const MODE_ALLOW = 'allow';
const MODE_DENY = 'deny';
const MODE_COLUMNS = 'allow_columns';

const tableKey = (kbId, table) => `${kbId}::${table}`;

/**
 * KB → table → column tree with per-node permission toggles.
 *
 * The default for every node is `allow` (no restriction). Only explicit
 * restrictions are emitted back through `onChange` as a flat policy list:
 *   [{ kb_id, table_name: null, mode: 'deny' }]                       // KB deny
 *   [{ kb_id, table_name: 'x', mode: 'deny' }]                        // table deny
 *   [{ kb_id, table_name: 'x', mode: 'allow_columns', column_allowlist: [...] }]
 */
export default function ResourceAccessPolicyTree({ kbs = [], activeKbId, policies = [], onChange }) {
  const { t } = useLanguage();
  const ap = t.accessPolicy || {};

  const [kbModes, setKbModes] = useState(() => {
    const m = {};
    for (const p of policies) if (!p.table_name) m[p.kb_id] = p.mode === MODE_DENY ? MODE_DENY : MODE_ALLOW;
    return m;
  });
  const [tableModes, setTableModes] = useState(() => {
    const m = {};
    for (const p of policies) if (p.table_name) m[tableKey(p.kb_id, p.table_name)] = p.mode;
    return m;
  });
  const [columnLists, setColumnLists] = useState(() => {
    const m = {};
    for (const p of policies)
      if (p.table_name && p.mode === MODE_COLUMNS) m[tableKey(p.kb_id, p.table_name)] = p.column_allowlist || [];
    return m;
  });

  function emit(nextKb, nextTable, nextCols) {
    const flat = [];
    for (const kb of kbs) {
      if (nextKb[kb.id] === MODE_DENY) {
        flat.push({ kb_id: kb.id, table_name: null, mode: MODE_DENY, column_allowlist: null });
        continue;
      }
      for (const tb of kb.tables || []) {
        const mode = nextTable[tableKey(kb.id, tb.name)] || MODE_ALLOW;
        if (mode === MODE_ALLOW) continue;
        flat.push({
          kb_id: kb.id,
          table_name: tb.name,
          mode,
          column_allowlist: mode === MODE_COLUMNS ? nextCols[tableKey(kb.id, tb.name)] || [] : null,
        });
      }
    }
    onChange?.(flat);
  }

  function setKbMode(kbId, mode) {
    const nextKb = { ...kbModes, [kbId]: mode };
    const nextTable = { ...tableModes };
    const nextCols = { ...columnLists };
    if (mode === MODE_DENY) {
      for (const tb of (kbs.find((k) => k.id === kbId)?.tables || [])) {
        delete nextTable[tableKey(kbId, tb.name)];
        delete nextCols[tableKey(kbId, tb.name)];
      }
    }
    setKbModes(nextKb);
    setTableModes(nextTable);
    setColumnLists(nextCols);
    emit(nextKb, nextTable, nextCols);
  }

  function setTableMode(kbId, table, mode) {
    const key = tableKey(kbId, table);
    const nextTable = { ...tableModes, [key]: mode };
    const nextCols = { ...columnLists };
    if (mode !== MODE_COLUMNS) delete nextCols[key];
    else if (!nextCols[key]) nextCols[key] = [];
    setTableModes(nextTable);
    setColumnLists(nextCols);
    emit(kbModes, nextTable, nextCols);
  }

  function toggleColumn(kbId, table, col) {
    const key = tableKey(kbId, table);
    const cur = columnLists[key] || [];
    const nextCols = { ...columnLists, [key]: cur.includes(col) ? cur.filter((c) => c !== col) : [...cur, col] };
    setColumnLists(nextCols);
    emit(kbModes, tableModes, nextCols);
  }

  if (!kbs.length) {
    return <p className="text-sm text-muted-foreground">{ap.noPolicyConfigured || 'No restrictions (allow all by default)'}</p>;
  }

  const activeKb = kbs.find((k) => k.id === activeKbId);
  if (!activeKb) {
    return <p className="text-sm text-muted-foreground">{ap.databases || 'No database selected'}</p>;
  }

  const kbMode = kbModes[activeKb.id] || MODE_ALLOW;
  const denied = kbMode === MODE_DENY;
  const tables = activeKb.tables || [];

  return (
    <div className="space-y-2">
      {/* DB header row: database name + DB-level Allow/Deny */}
      <div className={cn('rounded-lg border px-3 py-2.5 flex items-center justify-between gap-3', denied ? 'border-destructive/40 bg-destructive/5' : 'border-border bg-muted/30')}>
        <div className="flex items-center gap-2 min-w-0">
          <Database className={cn('h-4 w-4 shrink-0', denied ? 'text-destructive' : 'text-primary')} />
          <span className={cn('font-medium truncate text-sm', denied && 'text-destructive line-through')}>{activeKb.name}</span>
        </div>
        <RadioGroup
          value={kbMode}
          onValueChange={(v) => setKbMode(activeKb.id, v)}
          className="flex items-center gap-4"
        >
          <label className="flex items-center gap-1.5 text-xs cursor-pointer">
            <RadioGroupItem value={MODE_ALLOW} className="text-green-600" />
            <span>{ap.allow || 'Allow'}</span>
          </label>
          <label className="flex items-center gap-1.5 text-xs cursor-pointer">
            <RadioGroupItem value={MODE_DENY} className="text-red-600" />
            <span>{ap.deny || 'Deny'}</span>
          </label>
        </RadioGroup>
      </div>

      {denied && (
        <p className="text-xs text-muted-foreground">{ap.dbLevelDenyHint || 'Deny hides every table in this database.'}</p>
      )}

      {!denied && tables.length === 0 && (
        <p className="text-xs text-muted-foreground">{ap.noTables || 'This database has no tables indexed yet.'}</p>
      )}

      {/* Flat table list */}
      {!denied &&
        tables.map((tb) => {
          const key = tableKey(activeKb.id, tb.name);
          const mode = tableModes[key] || MODE_ALLOW;
          return (
            <div key={tb.name} className="rounded-md border border-border/70 bg-background p-2">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2 min-w-0">
                  <Table2 className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className={cn('text-sm truncate', mode === MODE_DENY && 'line-through text-destructive')}>{tb.name}</span>
                </div>
                <RadioGroup
                  value={mode}
                  onValueChange={(v) => setTableMode(activeKb.id, tb.name, v)}
                  className="flex items-center gap-3"
                >
                  <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                    <RadioGroupItem value={MODE_ALLOW} className="text-green-600" />
                    <span>{ap.allow || 'Allow'}</span>
                  </label>
                  <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                    <RadioGroupItem value={MODE_DENY} className="text-red-600" />
                    <span>{ap.deny || 'Deny'}</span>
                  </label>
                  <label className="flex items-center gap-1.5 text-xs cursor-pointer">
                    <RadioGroupItem value={MODE_COLUMNS} className="text-amber-600" />
                    <span>{ap.restrictColumns || 'Restrict'}</span>
                  </label>
                </RadioGroup>
              </div>

              {mode === MODE_COLUMNS && (
                <div className="mt-2 pl-4 border-l">
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                    <Columns3 className="h-3 w-3" />
                    <span>{ap.columnsToAllow || 'Columns to allow'}</span>
                  </div>
                  {tb.columns && tb.columns.length > 0 ? (
                    <div className="grid grid-cols-2 gap-1">
                      {tb.columns.map((col) => (
                        <label key={col} className="flex items-center gap-2 text-xs cursor-pointer py-0.5">
                          <Checkbox
                            checked={(columnLists[key] || []).includes(col)}
                            onCheckedChange={() => toggleColumn(activeKb.id, tb.name, col)}
                          />
                          <span className="truncate">{col}</span>
                        </label>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-muted-foreground italic">{ap.noPolicyConfigured || 'No columns available'}</p>
                  )}
                </div>
              )}
            </div>
          );
        })}
    </div>
  );
}
