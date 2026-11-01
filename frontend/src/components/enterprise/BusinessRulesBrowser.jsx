import { useEffect, useState } from 'react';
import { Loader2, ScrollText, ChevronLeft, Scale } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { toast } from 'sonner';
import { EVIDENCE_BADGE_STYLE } from '@/lib/evidenceStatuses';

/**
 * Phase 6F — Business Rules investigation (Business Knowledge area).
 *
 * Read view over the org's BusinessRule rows plus the admin review
 * lifecycle (existing /business-rules endpoints — no new backend).
 *
 * State vocabulary is presented WITHOUT conflation:
 *   Documented — recorded from authoritative company documents
 *   Discovered — proposed by AI discovery, awaiting human review
 *   Verified   — confirmed by a human reviewer (still informational)
 *   Enabled    — verified AND executable (gates actions via the marker
 *                protocol: PROHIBIT:/FORBIDDEN:/DENY:/REQUIRE_APPROVAL:)
 *   Observed   — seen in real executions; never promotes verification
 *
 * A rule's marker effect (blocks / needs approval) is shown ONLY as
 * metadata — actual enforcement happens at the governed-action boundary
 * for enabled rules, exactly as recorded by the deterministic pre-check.
 */

const STATES = [
  { term: 'Documented', def: 'recorded from authoritative company documents during onboarding' },
  { term: 'Discovered', def: 'found by AI discovery — becomes Proposed until reviewed' },
  { term: 'Proposed', def: 'awaiting a human decision — a hypothesis, never guidance' },
  { term: 'Verified', def: 'confirmed by a human reviewer — still informational until enabled' },
  { term: 'Enabled', def: 'verified AND executable — gates governed actions per the marker protocol' },
  { term: 'Observed', def: 'seen in real executions — evidence only, never promotes verification' },
  { term: 'Rejected', def: 'declined by a reviewer — excluded from AI context until resubmitted' },
];

const STATUS_CLS = {
  enabled: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  verified: 'border-sky-200 bg-sky-50 text-sky-700',
  proposed: 'border-stone-200 bg-stone-50 text-stone-500',
  rejected: 'border-red-200 bg-red-50 text-red-700',
};

const EFFECT_LABEL = {
  block: 'blocks execution',
  approval: 'needs approval',
  informational: 'informational',
};

function EffectBadge({ effect, status }) {
  if (effect === 'informational' || status !== 'enabled') {
    return (
      <span className="text-[9px] px-1.5 py-0.5 rounded-full border border-stone-200 bg-stone-50 text-stone-500">
        informational
      </span>
    );
  }
  const cls = effect === 'block'
    ? 'border-red-200 bg-red-50 text-red-700'
    : 'border-amber-200 bg-amber-50 text-amber-700';
  return (
    <span className={`text-[9px] px-1.5 py-0.5 rounded-full border ${cls}`}>
      {EFFECT_LABEL[effect] || 'informational'}
    </span>
  );
}

function StateGlossary() {
  return (
    <details className="rounded-lg border border-stone-200 bg-stone-50 px-3 py-2">
      <summary className="text-[10px] font-semibold text-stone-600 cursor-pointer">
        What do the states mean?
      </summary>
      <dl className="mt-1.5 space-y-1">
        {STATES.map((s) => (
          <div key={s.term} className="text-[10px]">
            <dt className="inline font-medium text-stone-700">{s.term}</dt>
            <dd className="inline text-stone-500"> — {s.def}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function RuleDetail({ ruleId, isAdmin, onOpenRule, onClose }) {
  const [rule, setRule] = useState(null);
  const [failed, setFailed] = useState(false);
  const [acting, setActing] = useState(false);

  const load = async () => {
    setFailed(false);
    try {
      const res = await authFetch(
        `/api/ontology/enterprise/business-rules/${ruleId}`);
      if (!res.ok) { setFailed(true); return; }
      setRule(await res.json());
    } catch { setFailed(true); }
  };
  useEffect(() => { setRule(null); load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [ruleId]);

  const review = async (action) => {
    setActing(true);
    try {
      const res = await authFetch(
        `/api/ontology/enterprise/business-rules/${ruleId}/${action}`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: `reviewed in Business Rules (Phase 6F UI)` }) });
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b.detail || `HTTP ${res.status}`);
      }
      toast.success(`Rule ${action}d`);
      await load();
    } catch (e) {
      toast.error(`Review failed: ${e.message || e}`);
    } finally { setActing(false); }
  };

  if (failed) {
    return (
      <div className="space-y-2">
        <button onClick={onClose}
          className="flex items-center gap-1 text-[11px] text-stone-500 hover:text-stone-800">
          <ChevronLeft size={12} /> All business rules
        </button>
        <div className="rounded-lg border border-dashed border-stone-300 bg-stone-50 px-3 py-4 text-stone-400">
          This business rule is not available — it may have been removed or
          belongs to another organization.
        </div>
      </div>
    );
  }
  if (!rule) {
    return <p className="flex items-center gap-1.5 text-stone-400 py-4">
      <Loader2 size={12} className="animate-spin" /> Loading rule…</p>;
  }

  const nextActions = [];
  if (rule.status === 'proposed') nextActions.push(['confirm', 'Confirm'], ['reject', 'Reject']);
  if (rule.status === 'verified') nextActions.push(['enable', 'Enable']);
  if (rule.status === 'enabled') nextActions.push(['disable', 'Disable']);
  if (rule.status === 'rejected') nextActions.push(['resubmit', 'Resubmit']);

  return (
    <div className="space-y-3">
      <button onClick={onClose}
        className="flex items-center gap-1 text-[11px] text-stone-500 hover:text-stone-800">
        <ChevronLeft size={12} /> All business rules
      </button>
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`text-[9px] px-1.5 py-0.5 rounded-full border capitalize ${(STATUS_CLS[rule.status]) || ''}`}>
          {rule.status}
        </span>
        <EffectBadge effect={rule.effect} status={rule.status} />
        <span className="text-[9px] text-stone-400 capitalize">
          scope: {rule.scope}{rule.scope_ref_id ? ` · ${rule.scope_ref_id}` : ''} · applies to: {rule.applies_to}
        </span>
      </div>
      <p className="text-[12px] text-stone-800 font-medium">{rule.statement}</p>
      {rule.status !== 'enabled' && (
        <p className="text-[10px] text-stone-400">
          This rule is not enabled — it records knowledge but does not gate
          any action. Only verified rules that an administrator enables, with
          an enforcement marker, are executable.
        </p>
      )}

      <div>
        <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Evidence sources</h4>
        {(rule.resolved_evidence || []).length === 0 ? (
          <p className="text-stone-400 mt-1">No evidence sources recorded for this rule.</p>
        ) : (
          <div className="mt-1 space-y-1">
            {rule.resolved_evidence.map((e) => (
              e.available ? (
                <div key={e.id} className="flex items-center gap-1.5 text-stone-700">
                  <Scale size={10} className="text-stone-400" />
                  <span>{e.source_ref}</span>
                  <span className={`text-[9px] px-1.5 py-0.5 rounded-full border ${EVIDENCE_BADGE_STYLE[e.evidence_status] || ''}`}>
                    {e.evidence_status}
                  </span>
                </div>
              ) : (
                <div key={e.id} className="text-[10px] text-stone-400">
                  evidence {e.id} — unavailable (may have been removed)
                </div>
              )
            ))}
          </div>
        )}
      </div>

      <div>
        <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">Review history</h4>
        {(rule.review_history || []).length === 0 ? (
          <p className="text-stone-400 mt-1">No review decisions recorded yet.</p>
        ) : (
          <div className="mt-1 space-y-0.5 text-[10px] text-stone-600">
            {rule.review_history.map((h, i) => (
              <div key={i}>
                {h.action} · {h.reviewer_name || 'unknown'}
                {h.at ? ` · ${new Date(h.at).toLocaleDateString()}` : ''}
              </div>
            ))}
          </div>
        )}
      </div>

      {isAdmin && nextActions.length > 0 && (
        <div className="flex items-center gap-1.5 pt-1">
          {nextActions.map(([action, label]) => (
            <button key={action} disabled={acting} onClick={() => review(action)}
              className="text-[10px] px-2 py-1 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-100 disabled:opacity-50">
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function BusinessRulesBrowser({ ruleId, isAdmin = false, onOpenRule, onClose }) {
  const [rules, setRules] = useState(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (ruleId) return; // detail view fetches its own
    let alive = true;
    (async () => {
      try {
        const q = statusFilter ? `?status=${statusFilter}&page_size=50` : '?page_size=50';
        const res = await authFetch(`/api/ontology/enterprise/business-rules${q}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        if (alive) setRules(body.items || []);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => { alive = false; };
  }, [ruleId, statusFilter]);

  if (ruleId) {
    return <RuleDetail ruleId={ruleId} isAdmin={isAdmin} onOpenRule={onOpenRule} onClose={onClose || (() => onOpenRule?.(null))} />;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <ScrollText size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">Business Rules</h2>
        <span className="text-[11px] text-stone-400">
          the rules that constrain decisions — informational knowledge vs executable guidance
        </span>
      </div>

      <StateGlossary />

      <div className="flex items-center gap-1 text-[10px]">
        {['', 'enabled', 'verified', 'proposed', 'rejected'].map((s) => (
          <button key={s || 'all'} onClick={() => setStatusFilter(s)}
            className={`px-2 py-0.5 rounded-md border capitalize ${
              statusFilter === s
                ? 'border-stone-500 bg-stone-100 text-stone-800 font-medium'
                : 'border-stone-200 bg-white text-stone-500 hover:bg-stone-50'}`}>
            {s || 'all'}
          </button>
        ))}
      </div>

      {failed ? (
        <p className="text-[11px] text-stone-400">Business rules are not available right now.</p>
      ) : !rules ? (
        <p className="flex items-center gap-1.5 text-stone-400 text-[11px]">
          <Loader2 size={12} className="animate-spin" /> Loading rules…</p>
      ) : rules.length === 0 ? (
        <p className="text-[11px] text-stone-400">
          No business rules recorded{statusFilter ? ` with status “${statusFilter}”` : ''} yet.
        </p>
      ) : (
        <div className="rounded-xl border border-stone-200 bg-white divide-y divide-stone-100">
          {rules.map((r) => (
            <button key={r.id}
              onClick={() => onOpenRule?.(r.id)}
              className="w-full text-left px-4 py-2.5 hover:bg-stone-50">
              <div className="flex items-center gap-2 flex-wrap">
                <span className={`text-[9px] px-1.5 py-0.5 rounded-full border capitalize ${(STATUS_CLS[r.status]) || ''}`}>
                  {r.status}
                </span>
                <EffectBadge effect={r.effect} status={r.status} />
                <span className="text-[9px] text-stone-400 capitalize ml-auto">
                  scope: {r.scope}
                </span>
              </div>
              <p className="mt-1 text-[12px] text-stone-800">{r.statement}</p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
