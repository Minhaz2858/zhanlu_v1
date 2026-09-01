/**
 * DataTableCard — renders database query results as an interactive table.
 *
 * Used by MessageBubble when an ask_data_agent tool call returns rows.
 * Features:
 * - Auto-derives columns from the first row's keys
 * - Sortable columns (click header to toggle sort)
 * - CSV export button
 * - Max 100 rows displayed (with "showing N of M" indicator)
 * - SQL display in collapsible <details>
 * - Source name badge
 */

import { useState, useMemo, useCallback } from 'react';
import { Table, Download, Database, ChevronUp, ChevronDown, ChevronRight, Copy, Check } from 'lucide-react';

const MAX_DISPLAY_ROWS = 100;

export default function DataTableCard({ rows, sql, source }) {
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState('asc'); // 'asc' | 'desc'
  const [copied, setCopied] = useState(false);

  // Derive columns from the first row
  const columns = useMemo(() => {
    if (!rows || rows.length === 0) return [];
    const firstRow = rows[0];
    if (typeof firstRow !== 'object' || firstRow === null) return [];
    return Object.keys(firstRow);
  }, [rows]);

  // Sort rows
  const sortedRows = useMemo(() => {
    if (!rows) return [];
    if (!sortCol) return rows;

    const sorted = [...rows];
    sorted.sort((a, b) => {
      const aVal = a?.[sortCol];
      const bVal = b?.[sortCol];

      // Handle nulls/undefined
      if (aVal == null) return 1;
      if (bVal == null) return -1;

      // Try numeric comparison
      const aNum = Number(aVal);
      const bNum = Number(bVal);
      if (!isNaN(aNum) && !isNaN(bNum)) {
        return sortDir === 'asc' ? aNum - bNum : bNum - aNum;
      }

      // String comparison
      const aStr = String(aVal);
      const bStr = String(bVal);
      if (sortDir === 'asc') return aStr.localeCompare(bStr);
      return bStr.localeCompare(aStr);
    });

    return sorted;
  }, [rows, sortCol, sortDir]);

  const displayRows = useMemo(() => sortedRows.slice(0, MAX_DISPLAY_ROWS), [sortedRows]);

  const handleSort = useCallback((col) => {
    if (sortCol === col) {
      setSortDir((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortCol(col);
      setSortDir('asc');
    }
  }, [sortCol]);

  const handleCSVExport = useCallback(() => {
    if (!rows || rows.length === 0) return;

    const escapeCSV = (val) => {
      if (val == null) return '';
      const str = String(val);
      if (str.includes(',') || str.includes('"') || str.includes('\n')) {
        return `"${str.replace(/"/g, '""')}"`;
      }
      return str;
    };

    const header = columns.map(escapeCSV).join(',');
    const dataLines = rows.map((row) =>
      columns.map((col) => escapeCSV(row?.[col])).join(',')
    );
    const csv = [header, ...dataLines].join('\n');

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'query_results.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [rows, columns]);

  const handleCopy = useCallback(() => {
    if (!rows || rows.length === 0) return;

    const header = columns.join('\t');
    const dataLines = rows.map((row) =>
      columns.map((col) => String(row?.[col] ?? '')).join('\t')
    );
    const tsv = [header, ...dataLines].join('\n');

    navigator.clipboard.writeText(tsv).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [rows, columns]);

  if (!rows || rows.length === 0) {
    return null;
  }

  return (
    <div className="my-3 rounded-lg border border-border bg-card overflow-hidden">
      {/* Header bar */}
      <div className="flex items-center justify-between gap-2 border-b border-border bg-secondary/40 px-3 py-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Table className="h-3.5 w-3.5" />
          <span className="font-medium text-foreground">
            {rows.length} {rows.length === 1 ? 'row' : 'rows'}
          </span>
          {columns.length > 0 && (
            <span className="text-muted-foreground/70">· {columns.length} columns</span>
          )}
          {source && (
            <span className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-medium text-blue-700">
              <Database className="h-2.5 w-2.5" />
              {source}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={handleCopy}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
            title="Copy as TSV (paste into Excel/Sheets)"
          >
            {copied ? <Check className="h-3 w-3 text-green-500" /> : <Copy className="h-3 w-3" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
          <button
            onClick={handleCSVExport}
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
            title="Download as CSV"
          >
            <Download className="h-3 w-3" />
            CSV
          </button>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 z-10">
            <tr className="bg-secondary/80 backdrop-blur">
              {columns.map((col) => (
                <th
                  key={col}
                  onClick={() => handleSort(col)}
                  className="cursor-pointer select-none whitespace-nowrap px-3 py-2 text-left font-semibold text-foreground hover:bg-secondary transition-colors border-b border-border"
                >
                  <div className="flex items-center gap-1">
                    <span>{col}</span>
                    {sortCol === col && (
                      sortDir === 'asc'
                        ? <ChevronUp className="h-3 w-3" />
                        : <ChevronDown className="h-3 w-3" />
                    )}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayRows.map((row, i) => (
              <tr
                key={i}
                className={i % 2 === 0 ? 'bg-card hover:bg-blue-50/50' : 'bg-secondary/20 hover:bg-blue-50/50'}
              >
                {columns.map((col) => {
                  const val = row?.[col];
                  const displayVal = val == null ? '' : typeof val === 'object' ? JSON.stringify(val) : String(val);
                  return (
                    <td
                      key={col}
                      className="whitespace-nowrap px-3 py-1.5 text-muted-foreground border-b border-border/50"
                      title={displayVal}
                    >
                      {displayVal.length > 80 ? displayVal.substring(0, 80) + '…' : displayVal}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Footer */}
      {sortedRows.length > MAX_DISPLAY_ROWS && (
        <div className="border-t border-border bg-secondary/30 px-3 py-1.5 text-[10px] text-muted-foreground text-center">
          Showing {MAX_DISPLAY_ROWS} of {sortedRows.length} rows. Export CSV to see all.
        </div>
      )}

      {/* SQL (collapsible) */}
      {sql && (
        <details className="border-t border-border">
          <summary className="cursor-pointer select-none px-3 py-2 text-xs text-muted-foreground hover:bg-secondary/40 transition-colors flex items-center gap-1">
            <ChevronRight className="h-3 w-3" />
            <span className="font-mono">View SQL query</span>
          </summary>
          <pre className="overflow-x-auto bg-secondary/20 px-3 py-2 text-[11px] font-mono text-foreground/80 whitespace-pre-wrap">
            {sql}
          </pre>
        </details>
      )}
    </div>
  );
}
