import { useState, useEffect, useCallback } from 'react';
import {
  ScrollText, Loader2, Plus, Pencil, Check, X, Trash2, Power,
} from 'lucide-react';
import { appParams } from '@/lib/app-params';
import authFetch from '@/api/authFetch';

const APP_ID = appParams.appId || 'local-zhanlu-app';

const KINDS = [
  { value: 'fiscal_calendar', label: 'Fiscal calendar' },
  { value: 'default_filter', label: 'Default filter' },
  { value: 'territory_map', label: 'Territory / org map' },
  { value: 'exception', label: 'Exception' },
  { value: 'policy', label: 'Reporting policy' },
  { value: 'other', label: 'Other rule' },
];

/**
 * Editable ORGANIZATIONAL RULES (OCI organizational enrichment) for a
 * database KnowledgeBase: fiscal-calendar rules, default filters, territory
 * mappings, exceptions, reporting policies. Authored rules are injected into
 * every NL2SQL generation as a BUSINESS RULES constraint block — tribal
 * knowledge made explicit and enforceable.
 */
export default function KbGuidelineEditor({ kbId }) {
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState({ kind: 'default_filter', title: '', content: '' });
  const [editingId, setEditingId] = useState(null);
  const [editDraft, setEditDraft] = useState({});

  const fetchRules = useCallback(async () => {
    try {
      const res = await authFetch(`/api/apps/${APP_ID}/knowledge_bases/${kbId}/guidelines`);
      if (!res.ok) {
        setError(`Failed to load (${res.status})`);
        setRules([]);
      } else {
        setRules(await res.json());
        setError(null);
      }
    } catch {
      setError('Failed to load');
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    fetchRules();
  }, [fetchRules]);

  async function addRule() {
    if (!draft.title.trim() || !draft.content.trim()) return;
    setSaving(true);
    try {
      const res = await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/guidelines`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(draft),
        }
      );
      if (res.ok) {
        setDraft({ kind: 'default_filter', title: '', content: '' });
        setAdding(false);
        await fetchRules();
      }
    } finally {
      setSaving(false);
    }
  }

  async function saveEdit() {
    setSaving(true);
    try {
      await authFetch(
        `/api/apps/${APP_ID}/knowledge_bases/${kbId}/guidelines/${editingId}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(editDraft),
        }
      );
      setEditingId(null);
      await fetchRules();
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled(r) {
    await authFetch(
      `/api/apps/${APP_ID}/knowledge_bases/${kbId}/guidelines/${r.id}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_enabled: !r.is_enabled }),
      }
    );
    await fetchRules();
  }

  async function removeRule(r) {
    if (!window.confirm(`Delete rule "${r.title}"?`)) return;
    await authFetch(
      `/api/apps/${APP_ID}/knowledge_bases/${kbId}/guidelines/${r.id}`,
      { method: 'DELETE' }
    );
    await fetchRules();
  }

  if (loading) {
    return (
      <div className="px-6 pt-5 flex items-center gap-2 text-sm text-stone-400">
        <Loader2 size={14} className="animate-spin" /> Loading guidelines…
      </div>
    );
  }

  return (
    <div className="px-6 py-5 border-t border-stone-100">
      <div className="flex items-center gap-2 flex-wrap">
        <ScrollText size={16} className="text-amber-600" />
        <span className="text-sm font-semibold text-stone-800">
          Business Rules & Guidelines
        </span>
        <span className="text-xs text-stone-400">
          fiscal calendar, default filters, org mappings — injected into every SQL generation
        </span>
        <button
          onClick={() => setAdding((v) => !v)}
          className="ml-auto flex items-center gap-1 text-xs px-2.5 py-1 rounded-md bg-amber-600 text-white hover:bg-amber-700"
        >
          <Plus size={13} /> Add rule
        </button>
      </div>

      {error && <p className="mt-3 text-xs text-red-600">{error}</p>}

      {adding && (
        <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50/40 p-3 space-y-2">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <select
              value={draft.kind}
              onChange={(e) => setDraft({ ...draft, kind: e.target.value })}
              className="text-[13px] border border-stone-300 rounded px-2 py-1.5"
            >
              {KINDS.map((k) => (
                <option key={k.value} value={k.value}>{k.label}</option>
              ))}
            </select>
            <input
              value={draft.title}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              placeholder="Rule title * (e.g. Exclude cancelled orders)"
              className="text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
            />
          </div>
          <textarea
            value={draft.content}
            onChange={(e) => setDraft({ ...draft, content: e.target.value })}
            placeholder="The rule, stated precisely (e.g. Revenue queries must filter status != 'CANCELLED'; the fiscal year starts April 1)"
            rows={2}
            className="w-full text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
          />
          <div className="flex gap-2">
            <button
              onClick={addRule}
              disabled={saving || !draft.title.trim() || !draft.content.trim()}
              className="flex items-center gap-1 text-xs px-3 py-1 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              <Check size={12} /> {saving ? 'Saving…' : 'Save rule'}
            </button>
            <button
              onClick={() => setAdding(false)}
              className="text-xs px-3 py-1 rounded border border-stone-300 text-stone-600 hover:bg-stone-100"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {rules.length === 0 && !adding ? (
        <p className="mt-3 text-xs text-stone-400">
          No rules authored yet. Write down what every analyst knows and every
          query must respect — fiscal year start, the filters that are always
          applied, which org maps to which territory.
        </p>
      ) : (
        <div className="mt-3 rounded-lg border border-stone-200 divide-y divide-stone-100 overflow-hidden">
          {rules.map((r) => (
            <div key={r.id} className="px-4 py-2.5 flex items-start gap-3">
              {editingId === r.id ? (
                <div className="flex-1 space-y-1.5">
                  <div className="grid grid-cols-2 gap-2">
                    <select
                      value={editDraft.kind}
                      onChange={(e) => setEditDraft({ ...editDraft, kind: e.target.value })}
                      className="text-[12px] border border-stone-300 rounded px-2 py-1"
                    >
                      {KINDS.map((k) => (
                        <option key={k.value} value={k.value}>{k.label}</option>
                      ))}
                    </select>
                    <input
                      value={editDraft.title || ''}
                      onChange={(e) => setEditDraft({ ...editDraft, title: e.target.value })}
                      className="text-[12px] border border-stone-300 rounded px-2 py-1"
                    />
                  </div>
                  <textarea
                    value={editDraft.content || ''}
                    onChange={(e) => setEditDraft({ ...editDraft, content: e.target.value })}
                    rows={2}
                    className="w-full text-[12px] border border-stone-300 rounded px-2 py-1"
                  />
                  <div className="flex gap-1">
                    <button
                      onClick={saveEdit}
                      disabled={saving}
                      className="flex items-center gap-1 text-[11px] px-2 py-0.5 rounded bg-emerald-600 text-white disabled:opacity-50"
                    >
                      <Check size={11} /> Save
                    </button>
                    <button
                      onClick={() => setEditingId(null)}
                      className="flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border border-stone-300 text-stone-600"
                    >
                      <X size={11} /> Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[10px] px-1.5 py-0.5 rounded border border-stone-200 text-stone-500">
                        {r.kind}
                      </span>
                      <span className="text-[13px] font-medium text-stone-800">
                        {r.title}
                      </span>
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded border ${
                          r.is_enabled
                            ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                            : 'border-stone-200 text-stone-400'
                        }`}
                      >
                        {r.is_enabled ? 'active' : 'disabled'}
                      </span>
                    </div>
                    <p className="text-xs text-stone-500 mt-0.5 whitespace-pre-wrap">
                      {r.content}
                    </p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button
                      onClick={() => toggleEnabled(r)}
                      title={r.is_enabled ? 'Disable' : 'Enable'}
                      className={`p-1 rounded ${
                        r.is_enabled
                          ? 'text-emerald-600 hover:bg-emerald-50'
                          : 'text-stone-300 hover:bg-stone-100'
                      }`}
                    >
                      <Power size={13} />
                    </button>
                    <button
                      onClick={() => {
                        setEditingId(r.id);
                        setEditDraft({ ...r });
                      }}
                      className="p-1 rounded text-stone-400 hover:text-amber-600 hover:bg-amber-50"
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      onClick={() => removeRule(r)}
                      className="p-1 rounded text-stone-400 hover:text-red-600 hover:bg-red-50"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
