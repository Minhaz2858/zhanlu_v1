import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import {
  Database, RefreshCw, AlertCircle, Table2, Loader2,
  Search, Pencil, X, Check, Save,
} from 'lucide-react';
import { appParams } from '@/lib/app-params';
import authFetch from '@/api/authFetch';
import { useLanguage } from '@/lib/LanguageProvider';

const APP_ID = appParams.appId || 'local-zhanlu-app';
const POLL_INTERVAL_MS = 5000;

const STATUS_COLORS = {
  ready: 'bg-amber-100 text-amber-800 border-amber-200',
  indexing: 'bg-amber-100 text-amber-800 border-amber-200',
  error: 'bg-red-100 text-red-700 border-red-200',
  pending: 'bg-slate-100 text-slate-600 border-slate-200',
};

const STATUS_LABEL = {
  ready: '已完成',
  indexing: '索引中',
  error: '失败',
  pending: '等待中',
};

function formatNumber(n) {
  if (n == null) return '—';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

export default function KbCatalogTables({ kbId, dbType, kbName }) {
  const { t } = useLanguage();

  const [tables, setTables] = useState([]);
  const [status, setStatus] = useState(null);
  const [itemCount, setItemCount] = useState(null);
  const [name, setName] = useState(kbName || '');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reindexing, setReindexing] = useState(false);
  const [search, setSearch] = useState('');
  const [editingTable, setEditingTable] = useState(null);
  const pollRef = useRef(null);

  const fetchTables = useCallback(async () => {
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/catalog/tables`
      );
      if (!res.ok) {
        if (res.status === 404) {
          setStatus('pending');
          setTables([]);
          setError(null);
          setLoading(false);
          return;
        }
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      setStatus(data.catalog_status);
      setItemCount(data.item_count);
      setTables(data.tables || []);
      setName(data.kb_name || '');
      setError(null);
      setLoading(false);
    } catch (err) {
      setError(err.message);
      setLoading(false);
    }
  }, [kbId]);

  // Initial load
  useEffect(() => {
    fetchTables();
  }, [fetchTables]);

  // Poll while indexing/pending
  useEffect(() => {
    if (status === 'indexing' || status === 'pending') {
      pollRef.current = setInterval(fetchTables, POLL_INTERVAL_MS);
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [status, fetchTables]);

  const handleReindex = async () => {
    setReindexing(true);
    setError(null);
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/catalog/reindex`,
        { method: 'POST' }
      );
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      setStatus('indexing');
      setTimeout(() => fetchTables(), 2000);
    } catch (err) {
      setError(err.message);
    } finally {
      setReindexing(false);
    }
  };

  const handleSaveEdit = async (tableId, descriptions) => {
    const res = await authFetch(
      `/api/apps/${APP_ID}/knowledge_bases/${kbId}/catalog/tables/${tableId}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(descriptions),
      }
    );
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    // Refresh list to show updated description
    await fetchTables();
  };

  // Filter tables by search query (table name, column names, descriptions)
  const filteredTables = useMemo(() => {
    if (!search.trim()) return tables;
    const q = search.trim().toLowerCase();
    return tables.filter((t) => {
      const inName = (t.table_name || '').toLowerCase().includes(q);
      const inSchema = (t.schema_name || '').toLowerCase().includes(q);
      const inZh = (t.description_zh || '').toLowerCase().includes(q);
      const inEn = (t.description_en || '').toLowerCase().includes(q);
      const inCols = (t.column_names || []).some(
        (c) => (c || '').toLowerCase().includes(q)
      );
      return inName || inSchema || inZh || inEn || inCols;
    });
  }, [tables, search]);

  if (loading) {
    return (
      <div className="bg-white rounded-2xl border border-stone-200 p-8 shadow-sm">
        <div className="flex items-center gap-3 text-stone-400">
          <Loader2 size={20} className="animate-spin" />
          <span className="text-sm">加载中…</span>
        </div>
      </div>
    );
  }

  const statusLabel = STATUS_LABEL[status] || status || '等待中';
  const statusColor = STATUS_COLORS[status] || STATUS_COLORS.pending;
  const tableCount = tables.length;

  return (
    <div className="bg-white rounded-2xl border border-stone-200 shadow-sm overflow-hidden">
      {/* header */}
      <div className="px-6 pt-5 pb-4 flex items-center gap-2.5 border-b border-stone-100">
        <div className="w-9 h-9 rounded-xl bg-amber-50 border border-amber-100 flex items-center justify-center shrink-0">
          <Database size={18} className="text-amber-600" />
        </div>
        <h2 className="text-base font-semibold text-stone-800">目录</h2>
      </div>

      {/* search bar */}
      <div className="px-6 py-4 flex items-center gap-3 border-b border-stone-100">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-stone-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索表名、字段、描述..."
            className="w-full pl-10 pr-4 py-2.5 text-sm bg-stone-50 border border-stone-200 rounded-xl text-stone-700 placeholder:text-stone-400 focus:outline-none focus:border-amber-300 focus:bg-white transition-colors"
          />
        </div>
        <button
          onClick={handleReindex}
          disabled={reindexing || status === 'indexing'}
          className="inline-flex items-center gap-1.5 px-4 py-2.5 rounded-xl text-sm font-medium text-stone-600 bg-white border border-stone-200 hover:bg-stone-50 hover:text-stone-800 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
        >
          <RefreshCw
            size={15}
            className={reindexing || status === 'indexing' ? 'animate-spin' : ''}
          />
          刷新
        </button>
      </div>

      {/* KB name + status row */}
      <div className="px-6 py-4 flex items-center gap-3 border-b border-stone-100">
        <div className="w-7 h-7 rounded-lg bg-stone-100 flex items-center justify-center shrink-0">
          <Database size={14} className="text-stone-500" />
        </div>
        <span className="text-sm font-medium text-stone-800">{name || '未命名数据库'}</span>
        <span
          className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${statusColor}`}
        >
          {status === 'indexing' && (
            <Loader2 size={11} className="animate-spin" />
          )}
          {statusLabel}
        </span>
      </div>

      {/* error state */}
      {error && (
        <div className="mx-6 my-4 flex items-start gap-2.5 p-3 rounded-lg bg-red-50 border border-red-100">
          <AlertCircle size={16} className="text-red-500 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p className="text-xs text-red-700">加载目录失败</p>
            <p className="text-xs text-red-400 mt-0.5 font-mono">{error}</p>
          </div>
          <button
            onClick={fetchTables}
            className="text-xs text-red-600 hover:text-red-800 font-medium shrink-0"
          >
            重试
          </button>
        </div>
      )}

      {/* empty state */}
      {!error && tableCount === 0 && (
        <div className="flex flex-col items-center justify-center py-12 px-6 text-center">
          <div className="w-12 h-12 rounded-full bg-stone-50 border-2 border-dashed border-stone-200 flex items-center justify-center mb-3">
            <Database size={22} className="text-stone-300" />
          </div>
          <p className="text-sm text-stone-500">尚未发现数据表</p>
          <button
            onClick={handleReindex}
            disabled={reindexing || status === 'indexing'}
            className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-amber-700 bg-amber-50 border border-amber-100 hover:bg-amber-100 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <RefreshCw size={13} className={reindexing ? 'animate-spin' : ''} />
            刷新索引
          </button>
        </div>
      )}

      {/* table list */}
      {!error && tableCount > 0 && (
        <div>
          {/* section heading */}
          <div className="px-6 pt-5 pb-3">
            <h3 className="text-sm font-semibold text-stone-700">
              数据表 <span className="text-stone-400 font-normal">({filteredTables.length}{search.trim() ? ` / ${tableCount}` : ''})</span>
            </h3>
          </div>

          {/* table header */}
          <div className="grid grid-cols-[minmax(0,1.5fr)_minmax(0,3fr)_120px_40px] gap-4 px-6 py-2.5 border-y border-stone-100 bg-stone-50/40 text-xs font-medium text-stone-500 uppercase tracking-wide">
            <div>表名</div>
            <div>描述</div>
            <div className="text-right">行数</div>
            <div></div>
          </div>

          {/* table rows */}
          <div className="divide-y divide-stone-100">
            {filteredTables.map((tbl) => (
              <div
                key={tbl.id}
                className="grid grid-cols-[minmax(0,1.5fr)_minmax(0,3fr)_120px_40px] gap-4 px-6 py-3.5 hover:bg-stone-50/60 transition-colors group items-center"
              >
                {/* table name */}
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    {tbl.table_type === 'view' && (
                      <span className="text-[10px] font-medium text-stone-400 px-1.5 py-0.5 rounded border border-stone-200 bg-stone-50">
                        view
                      </span>
                    )}
                    {tbl.schema_name && tbl.schema_name !== 'public' && (
                      <span className="text-xs text-stone-400 font-normal shrink-0">
                        {tbl.schema_name}.
                      </span>
                    )}
                    <p className="font-mono text-[13px] font-medium text-stone-800 truncate">
                      {tbl.table_name}
                    </p>
                  </div>
                </div>

                {/* description */}
                <div className="min-w-0">
                  <p className="text-sm text-stone-600 leading-relaxed line-clamp-2">
                    {tbl.description_zh || tbl.description_en || tbl.table_name}
                  </p>
                </div>

                {/* row count */}
                <div className="text-right">
                  <span className="text-sm text-stone-600 tabular-nums">
                    {formatNumber(tbl.row_count)}
                  </span>
                </div>

                {/* edit pencil */}
                <div className="flex justify-end">
                  <button
                    onClick={() => setEditingTable(tbl)}
                    className="p-1.5 rounded-md text-stone-300 hover:text-amber-600 hover:bg-amber-50 transition-colors"
                    title="编辑描述"
                  >
                    <Pencil size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>

          {/* no search results */}
          {filteredTables.length === 0 && (
            <div className="py-10 px-6 text-center text-sm text-stone-400">
              没有匹配的表
            </div>
          )}
        </div>
      )}

      {/* edit modal */}
      {editingTable && (
        <EditDescriptionModal
          table={editingTable}
          onClose={() => setEditingTable(null)}
          onSave={handleSaveEdit}
        />
      )}
    </div>
  );
}


// ── edit modal ──────────────────────────────────────────────────────────────

function EditDescriptionModal({ table, onClose, onSave }) {
  const [zh, setZh] = useState(table.description_zh || '');
  const [en, setEn] = useState(table.description_en || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const handleSubmit = async (e) => {
    e?.preventDefault?.();
    setSaving(true);
    setError(null);
    try {
      await onSave(table.id, { description_zh: zh, description_en: en });
      onClose();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-stone-900/30 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-2xl shadow-xl border border-stone-200 w-full max-w-xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-stone-100">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-amber-50 border border-amber-100 flex items-center justify-center">
              <Pencil size={15} className="text-amber-600" />
            </div>
            <h3 className="text-sm font-semibold text-stone-800">编辑描述</h3>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-md text-stone-400 hover:text-stone-700 hover:bg-stone-100 transition-colors"
          >
            <X size={16} />
          </button>
        </div>

        {/* body */}
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {/* table name (read-only) */}
          <div>
            <label className="block text-xs font-medium text-stone-500 mb-1.5">
              表名
            </label>
            <div className="px-3 py-2 text-sm font-mono text-stone-700 bg-stone-50 border border-stone-200 rounded-lg">
              {table.schema_name && table.schema_name !== 'public' ? (
                <span className="text-stone-400">{table.schema_name}.</span>
              ) : null}
              {table.table_name}
            </div>
          </div>

          {/* zh description */}
          <div>
            <label className="block text-xs font-medium text-stone-500 mb-1.5">
              中文描述
            </label>
            <textarea
              value={zh}
              onChange={(e) => setZh(e.target.value)}
              rows={3}
              placeholder="用中文描述这张表的业务含义..."
              className="w-full px-3 py-2 text-sm bg-white border border-stone-200 rounded-lg text-stone-700 placeholder:text-stone-400 focus:outline-none focus:border-amber-300 focus:ring-1 focus:ring-amber-200 resize-none"
            />
          </div>

          {/* en description */}
          <div>
            <label className="block text-xs font-medium text-stone-500 mb-1.5">
              English description
            </label>
            <textarea
              value={en}
              onChange={(e) => setEn(e.target.value)}
              rows={3}
              placeholder="Describe the table's business meaning in English..."
              className="w-full px-3 py-2 text-sm bg-white border border-stone-200 rounded-lg text-stone-700 placeholder:text-stone-400 focus:outline-none focus:border-amber-300 focus:ring-1 focus:ring-amber-200 resize-none"
            />
          </div>

          {/* error */}
          {error && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-red-50 border border-red-100">
              <AlertCircle size={14} className="text-red-500 mt-0.5 shrink-0" />
              <p className="text-xs text-red-700">保存失败：{error}</p>
            </div>
          )}
        </form>

        {/* footer */}
        <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-stone-100 bg-stone-50/40">
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium text-stone-600 hover:text-stone-800 hover:bg-stone-100 transition-colors"
          >
            <X size={14} />
            取消
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={saving}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium text-white bg-amber-600 hover:bg-amber-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {saving ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Save size={14} />
            )}
            保存
          </button>
        </div>
      </div>
    </div>
  );
}