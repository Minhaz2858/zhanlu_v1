import { useState, useEffect, useCallback } from 'react';
import {
  BookOpen, Loader2, Plus, Pencil, Check, X, Trash2, Power,
} from 'lucide-react';
import { appParams } from '@/lib/app-params';
import authFetch from '@/api/authFetch';

const APP_ID = appParams.appId || 'local-zhanlu-app';

/**
 * Editable business glossary (VERIFIED tier) for a database KnowledgeBase.
 *
 * These are the terms the NL2SQL schema linking uses to resolve business
 * words ("发泡剂", "net revenue", "healthy accounts") before any SQL is
 * written — authoring here is the highest-leverage semantic edit an analyst
 * can make: entries land in the VERIFIED tier and immediately ground
 * retrieval, linking and generation.
 *
 * Backed by the Terminology authoring API:
 *   GET/POST /api/datasources/{kb_id}/terminologies
 *   PATCH/DELETE /api/datasources/{kb_id}/terminologies/{id}
 */
export default function KbGlossaryEditor({ kbId }) {
  const [terms, setTerms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ word: '', description: '' });
  const [editingId, setEditingId] = useState(null);
  const [editDraft, setEditDraft] = useState({ word: '', description: '' });
  const [saving, setSaving] = useState(false);

  const fetchTerms = useCallback(async () => {
    try {
      const res = await authFetch(
        `/api/datasources/${kbId}/terminologies`
      );
      if (!res.ok) {
        setError(`Failed to load (${res.status})`);
        setTerms([]);
      } else {
        setTerms(await res.json());
        setError(null);
      }
    } catch {
      setError('Failed to load');
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    fetchTerms();
  }, [fetchTerms]);

  async function addTerm() {
    if (!draft.word.trim()) return;
    setSaving(true);
    try {
      const res = await authFetch(
        `/api/datasources/${kbId}/terminologies`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            word: draft.word.trim(),
            description: draft.description.trim(),
          }),
        }
      );
      if (res.ok) {
        setDraft({ word: '', description: '' });
        setAdding(false);
        await fetchTerms();
      }
    } finally {
      setSaving(false);
    }
  }

  async function saveEdit() {
    if (!editDraft.word.trim()) return;
    setSaving(true);
    try {
      // NOTE: the PATCH endpoint updates description/enabled only — the word
      // is the upsert key. To rename, delete + re-add.
      const res = await authFetch(
        `/api/datasources/${kbId}/terminologies/${editingId}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            description: editDraft.description.trim(),
          }),
        }
      );
      if (res.ok) {
        setEditingId(null);
        await fetchTerms();
      }
    } finally {
      setSaving(false);
    }
  }

  async function toggleEnabled(t) {
    await authFetch(`/api/datasources/${kbId}/terminologies/${t.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !t.enabled }),
    });
    await fetchTerms();
  }

  async function removeTerm(t) {
    if (!window.confirm(`Delete term "${t.word}"?`)) return;
    await authFetch(`/api/datasources/${kbId}/terminologies/${t.id}`, {
      method: 'DELETE',
    });
    await fetchTerms();
  }

  if (loading) {
    return (
      <div className="px-6 pt-5 flex items-center gap-2 text-sm text-stone-400">
        <Loader2 size={14} className="animate-spin" /> Loading glossary…
      </div>
    );
  }

  return (
    <div className="px-6 py-5">
      <div className="flex items-center gap-2 flex-wrap">
        <BookOpen size={16} className="text-amber-600" />
        <span className="text-sm font-semibold text-stone-800">
          Business Terms & Metrics
        </span>
        <span className="text-xs text-stone-400">
          VERIFIED vocabulary — schema linking resolves business words from here
        </span>
        <button
          onClick={() => setAdding((v) => !v)}
          className="ml-auto flex items-center gap-1 text-xs px-2.5 py-1 rounded-md bg-amber-600 text-white hover:bg-amber-700"
        >
          <Plus size={13} /> Add term
        </button>
      </div>

      {error && (
        <p className="mt-3 text-xs text-red-600">{error}</p>
      )}

      {adding && (
        <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50/40 p-3 space-y-2">
          <input
            value={draft.word}
            onChange={(e) => setDraft({ ...draft, word: e.target.value })}
            placeholder="Term (e.g. net revenue / 净额 / 发泡剂)"
            className="w-full text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
          />
          <textarea
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="What it means for this business (definition, formula, thresholds…)"
            rows={2}
            className="w-full text-[13px] border border-stone-300 rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-400"
          />
          <div className="flex gap-2">
            <button
              onClick={addTerm}
              disabled={saving || !draft.word.trim()}
              className="flex items-center gap-1 text-xs px-3 py-1 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
            >
              <Check size={12} /> {saving ? 'Saving…' : 'Save term'}
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

      {terms.length === 0 && !adding ? (
        <p className="mt-3 text-xs text-stone-400">
          No terms authored yet. Add the business words your analysts use —
          each one becomes VERIFIED-tier knowledge that resolves ambiguous
          questions before SQL is written.
        </p>
      ) : (
        <div className="mt-3 rounded-lg border border-stone-200 divide-y divide-stone-100 overflow-hidden">
          {terms.map((t) => (
            <div key={t.id} className="px-4 py-2.5 flex items-start gap-3">
              {editingId === t.id ? (
                <div className="flex-1 space-y-1.5">
                  <input
                    value={editDraft.word}
                    readOnly
                    title="Term is the key — to rename, delete and re-add"
                    className="w-full text-[13px] border border-stone-200 bg-stone-50 rounded px-2 py-1 text-stone-500"
                  />
                  <textarea
                    value={editDraft.description}
                    onChange={(e) =>
                      setEditDraft({ ...editDraft, description: e.target.value })
                    }
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
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-medium text-stone-800">
                        {t.word}
                      </span>
                      <span
                        className={`text-[10px] px-1.5 py-0.5 rounded border ${
                          t.enabled
                            ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                            : 'border-stone-200 text-stone-400'
                        }`}
                      >
                        {t.enabled ? 'VERIFIED · active' : 'disabled'}
                      </span>
                    </div>
                    <p className="text-xs text-stone-500 mt-0.5 whitespace-pre-wrap">
                      {t.description || '—'}
                    </p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button
                      onClick={() => toggleEnabled(t)}
                      title={t.enabled ? 'Disable' : 'Enable'}
                      className={`p-1 rounded ${
                        t.enabled
                          ? 'text-emerald-600 hover:bg-emerald-50'
                          : 'text-stone-300 hover:bg-stone-100'
                      }`}
                    >
                      <Power size={13} />
                    </button>
                    <button
                      onClick={() => {
                        setEditingId(t.id);
                        setEditDraft({ word: t.word, description: t.description || '' });
                      }}
                      className="p-1 rounded text-stone-400 hover:text-amber-600 hover:bg-amber-50"
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      onClick={() => removeTerm(t)}
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
