import { useEffect, useState, useCallback } from 'react';
import { Loader2, CheckCircle2, AlertTriangle, X, ClipboardCheck, Search, FileText } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { toast } from 'sonner';

/**
 * Review Center (EBPM Increment 5) — administrator-facing central review
 * queue for EVERY AI proposal awaiting a human decision: org units,
 * capabilities, processes, activities and evidence rows. Confirm/reject
 * with reason; every decision is audited server-side and AI provenance is
 * preserved. Nothing is ever auto-promoted; sections can be honestly empty.
 */

const KIND_META = {
  org_unit: { label: 'Org unit', icon: ClipboardCheck },
  capability: { label: 'Capability', icon: ClipboardCheck },
  process: { label: 'Process', icon: ClipboardCheck },
  activity: { label: 'Activity', icon: ClipboardCheck },
  evidence: { label: 'Evidence', icon: FileText },
  // Phase 7B — ontology relationships + document-derived knowledge join the
  // SAME queue: one review center, every AI proposal, one audit trail.
  link: { label: 'Relationship', icon: ClipboardCheck },
  document_proposal: { label: 'Document proposal', icon: FileText },
};
const SECTIONS = [
  ['org_units', 'org_unit', 'Proposed departments & units'],
  ['capabilities', 'capability', 'Proposed capabilities'],
  ['processes', 'process', 'Proposed processes'],
  ['activities', 'activity', 'Proposed activities'],
  ['evidence', 'evidence', 'Evidence requiring review'],
  ['links', 'link', 'Proposed relationships'],
  ['document_proposals', 'document_proposal', 'Document-derived proposals'],
];
const SECTION_KEY_BY_KIND = {
  org_unit: 'org_units', capability: 'capabilities',
  process: 'processes', activity: 'activities', evidence: 'evidence',
  link: 'links', document_proposal: 'document_proposals',
};
// Document-derived knowledge is reviewed through its own lifecycle routes
// (atomic approve/reject service with decision events); everything else
// uses the generic enterprise confirm/reject endpoints.
const REVIEW_URL = (kind, id, decision) => kind === 'document_proposal'
  ? `/api/ontology/document-evidence/${id}/${decision === 'confirm' ? 'approve' : 'reject'}`
  : `/api/ontology/enterprise/${kind}/${id}/${decision}`;

export default function ReviewCenter({ onOpenProcess }) {
  const [queue, setQueue] = useState(null);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [rejecting, setRejecting] = useState(null); // `${kind}:${id}`
  const [reason, setReason] = useState('');
  const [q, setQ] = useState('');

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await authFetch('/api/ontology/enterprise/review-queue');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setQueue(await res.json());
    } catch (e) { setError(e.message || String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const review = async (kind, id, decision) => {
    setBusyId(`${kind}:${id}`);
    try {
      const res = await authFetch(REVIEW_URL(kind, id, decision),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: decision === 'reject' ? (reason || null) : null }),
        },
      );
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b.detail || `HTTP ${res.status}`);
      }
      toast.success(decision === 'confirm' ? 'Confirmed (verified)' : 'Rejected');
      setRejecting(null);
      setReason('');
      setQueue((prev) => {
        if (!prev) return prev;
        const sectionKey = SECTION_KEY_BY_KIND[kind];
        const next = {
          ...prev,
          [sectionKey]: (prev[sectionKey] || []).filter((i) => i.id !== id),
        };
        next.counts = { ...prev.counts, [sectionKey]: next[sectionKey].length };
        return next;
      });
    } catch (e) { toast.error(`Review failed: ${e.message || e}`); }
    finally { setBusyId(null); }
  };

  if (error) {
    return (
      <div className="text-[12px] text-red-600 bg-red-50 border border-red-200 rounded-md px-3 py-2">
        Failed to load the review queue: {error}
      </div>
    );
  }
  if (!queue) {
    return (
      <div className="flex items-center justify-center py-20 text-stone-400 text-[12px]">
        <Loader2 size={16} className="animate-spin mr-2" /> Loading review queue…
      </div>
    );
  }

  const total = Object.values(queue.counts || {}).reduce((a, b) => a + b, 0);
  const needle = q.trim().toLowerCase();
  const match = (item) => !needle
    || (item.label || '').toLowerCase().includes(needle)
    || (item.source_ref || '').toLowerCase().includes(needle)
    || (item.quote || '').toLowerCase().includes(needle);

  return (
    <div>
      <div className="flex items-center gap-2">
        <ClipboardCheck size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">Review Center</h2>
        <span className="text-[11px] text-stone-400">
          AI proposals awaiting human verification — confirm only what the evidence supports
        </span>
        <span className="ml-auto text-[11px] text-stone-400">{total} pending</span>
      </div>

      <div className="mt-3 relative">
        <Search size={13} className="absolute left-2.5 top-2.5 text-stone-400" />
        <input value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="Filter by label, source or quote…"
          className="text-[12px] pl-8 pr-3 py-2 w-72 border border-stone-300 rounded-md bg-white focus:outline-none focus:ring-1 focus:ring-stone-400" />
      </div>

      {(queue.hidden_document_proposals || 0) > 0 && (
        <p className="mt-2 text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-3 py-1.5">
          {queue.hidden_document_proposals} document proposal
          {queue.hidden_document_proposals === 1 ? '' : 's'} hidden — your account
          lacks access to the source document(s). Ask an owner for access to review them.
        </p>
      )}

      {total === 0 ? (
        <div className="mt-5 rounded-xl border border-dashed border-stone-300 bg-white px-6 py-12 text-center">
          <CheckCircle2 size={20} className="mx-auto text-emerald-300" />
          <p className="mt-2 text-[13px] text-stone-500">Review queue is empty.</p>
          <p className="mt-1 text-[11px] text-stone-400">
            Run AI discovery — new proposals will appear here for verification.
          </p>
        </div>
      ) : (
        <div className="mt-5 space-y-6">
          {SECTIONS.map(([key, kind, title]) => {
            const items = (queue[key] || []).filter(match);
            if (!items.length) return null;
            const meta = KIND_META[kind];
            return (
              <section key={key}>
                <h3 className="text-[11px] font-medium text-stone-400 uppercase tracking-wide mb-2">
                  {title} ({items.length})
                </h3>
                <div className="space-y-2">
                  {items.map((item) => {
                    const isRejecting = rejecting === `${kind}:${item.id}`;
                    const busy = busyId === `${kind}:${item.id}`;
                    const Icon = meta.icon;
                    return (
                      <div key={item.id}
                        className="rounded-xl border border-stone-200 bg-white px-4 py-3">
                        <div className="flex items-center gap-2 flex-wrap">
                          <Icon size={12} className="text-stone-400" />
                          <span className="text-[13px] font-medium text-stone-800">{item.label}</span>
                          {item.discovery_status && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-sky-200 bg-sky-50 text-sky-700">
                              {item.discovery_status}
                            </span>
                          )}
                          {item.evidence_kind && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-stone-200 bg-stone-50 text-stone-500">
                              {item.evidence_kind}
                            </span>
                          )}
                          {item.confidence != null && (
                            <span className="text-[10px] text-stone-400">
                              confidence {Math.round(item.confidence * 100)}%
                            </span>
                          )}
                          {item.evidence_ids?.length > 0 && (
                            <span className="text-[10px] text-stone-400" title={item.evidence_ids.join(', ')}>
                              {item.evidence_ids.length} evidence ref(s)
                            </span>
                          )}
                          {(kind === 'process' || kind === 'activity') && onOpenProcess && (
                            <button onClick={() => onOpenProcess(item.process_id || item.id, true)}
                              className="ml-auto text-[10px] px-2 py-0.5 rounded-full border border-stone-300 bg-white text-stone-600 hover:bg-stone-100">
                              open flow
                            </button>
                          )}
                        </div>

                        {item.unit_type && <p className="mt-0.5 text-[11px] text-stone-400 capitalize">{item.unit_type}</p>}
                        {item.description && <p className="mt-0.5 text-[11px] text-stone-500">{item.description}</p>}
                        {item.responsibilities && <p className="mt-0.5 text-[11px] text-stone-500">{item.responsibilities}</p>}
                        {item.purpose && <p className="mt-0.5 text-[11px] text-stone-500">{item.purpose}</p>}
                        {item.performer && <p className="mt-0.5 text-[11px] text-stone-500">performed by: {item.performer}</p>}
                        {item.quote && (
                          <p className="mt-1 text-[11px] text-stone-600 italic line-clamp-2">“{item.quote}”</p>
                        )}
                        {item.location_json?.page != null && (
                          <p className="mt-0.5 text-[10px] text-stone-400">location: page {item.location_json.page}</p>
                        )}
                        {item.source_ref && (
                          <p className="mt-0.5 text-[10px] text-stone-400">source: {item.source_ref}</p>
                        )}

                        <div className="mt-2.5">
                          {!isRejecting ? (
                            <div className="flex gap-2">
                              <button onClick={() => review(kind, item.id, 'confirm')} disabled={busy}
                                className="flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-md bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50">
                                {busy ? <Loader2 size={11} className="animate-spin" /> : <CheckCircle2 size={11} />} Confirm
                              </button>
                              <button onClick={() => { setRejecting(`${kind}:${item.id}`); setReason(''); }}
                                disabled={busy}
                                className="flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-md border border-red-200 text-red-600 bg-white hover:bg-red-50 disabled:opacity-50">
                                <AlertTriangle size={11} /> Reject
                              </button>
                            </div>
                          ) : (
                            <div className="space-y-1.5">
                              <textarea value={reason} onChange={(e) => setReason(e.target.value)}
                                rows={2}
                                placeholder="Reason for rejection (recorded in the audit trail)…"
                                className="w-full text-[11px] border border-stone-300 rounded-md px-2.5 py-1.5 focus:outline-none focus:ring-1 focus:ring-stone-400" />
                              <div className="flex gap-2">
                                <button onClick={() => review(kind, item.id, 'reject')} disabled={busy}
                                  className="text-[11px] px-2.5 py-1 rounded-md bg-red-600 text-white hover:bg-red-700 disabled:opacity-50">
                                  Reject
                                </button>
                                <button onClick={() => setRejecting(null)}
                                  className="flex items-center gap-1 text-[11px] px-2.5 py-1 rounded-md border border-stone-300 text-stone-600 hover:bg-stone-100">
                                  <X size={10} /> Cancel
                                </button>
                              </div>
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
