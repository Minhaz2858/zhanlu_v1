import { useEffect, useState } from 'react';
import { Loader2, Bot, Sparkles } from 'lucide-react';
import { authFetch } from '@/api/authFetch';
import { toast } from 'sonner';

/**
 * Phase 6F — AI Workforce (explainability view).
 *
 * Shows which enterprise context an assistant works from by calling the
 * EXISTING Business Context Service builder through the read-only
 * /enterprise/assistant-context endpoint — no second context system, no
 * duplicated BCS logic. Department association is metadata ("context
 * only"), never an access grant; ResourceAccessPolicy and project
 * authorization remain authoritative and their boundaries are shown,
 * not hidden.
 */

const STATUS_CLS = {
  complete: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  partial: 'border-amber-200 bg-amber-50 text-amber-700',
  unavailable: 'border-red-200 bg-red-50 text-red-700',
};

function ContextSection({ title, children }) {
  return (
    <div className="rounded-lg border border-stone-200 px-3 py-2">
      <h4 className="text-[10px] uppercase tracking-wide text-stone-400 font-semibold">{title}</h4>
      <div className="mt-1 text-[11px]">{children}</div>
    </div>
  );
}

export default function WorkforcePanel() {
  const [bindings, setBindings] = useState(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState('');
  const [ctx, setCtx] = useState(null);
  const [loadingCtx, setLoadingCtx] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await authFetch('/api/ontology/bindings');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = await res.json();
        if (alive) setBindings(Array.isArray(body) ? body : []);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => { alive = false; };
  }, []);

  const explain = async () => {
    setLoadingCtx(true);
    try {
      const q = query.trim() ? `&query=${encodeURIComponent(query.trim())}` : '';
      const res = await authFetch(
        `/api/ontology/enterprise/assistant-context?budget=2000${q}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setCtx(await res.json());
    } catch (e) {
      toast.error(`Context explanation failed: ${e.message || e}`);
    } finally { setLoadingCtx(false); }
  };

  const enabled = (ctx?.business_rules || []).filter((r) => r.status === 'enabled');
  const verified = (ctx?.business_rules || []).filter((r) => r.status === 'verified');
  const proposed = (ctx?.business_rules || []).filter(
    (r) => r.status !== 'enabled' && r.status !== 'verified');

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Bot size={15} className="text-stone-600" />
        <h2 className="text-sm font-semibold text-stone-800">AI Workforce</h2>
        <span className="text-[11px] text-stone-400">
          which enterprise knowledge informs each assistant — and what it may reach
        </span>
      </div>

      {failed ? (
        <p className="text-[11px] text-stone-400">Assistant bindings are not available right now.</p>
      ) : !bindings ? (
        <p className="flex items-center gap-1.5 text-[11px] text-stone-400">
          <Loader2 size={12} className="animate-spin" /> Loading assistants…</p>
      ) : bindings.length === 0 ? (
        <p className="text-[11px] text-stone-400">
          No agent-domain bindings recorded yet — assistants appear here once
          an administrator binds them to business domains.
        </p>
      ) : (
        <div className="rounded-xl border border-stone-200 bg-white divide-y divide-stone-100">
          {bindings.map((b) => (
            <div key={b.id} className="px-4 py-2.5 flex items-center gap-2 flex-wrap text-[11px]">
              <Bot size={11} className="text-stone-400" />
              <span className="font-medium text-stone-800">{b.agent_name}</span>
              <span className="text-stone-500">→ {b.domain_name}</span>
              <span className={`ml-auto text-[9px] px-1.5 py-0.5 rounded-full border ${
                b.permission === 'write'
                  ? 'border-amber-200 bg-amber-50 text-amber-700'
                  : 'border-sky-200 bg-sky-50 text-sky-700'}`}>
                {b.permission || 'read'}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Context explainer — reuses the BCS builder, no duplication */}
      <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
        <h3 className="text-[12px] font-semibold text-stone-800 flex items-center gap-1.5">
          <Sparkles size={12} className="text-stone-500" /> What does an assistant see?
        </h3>
        <p className="mt-1 text-[10px] text-stone-400">
          The same governed business context the runtime assembles — processes,
          objects, terminology and rules — with honest completeness.
        </p>
        <div className="mt-2 flex items-center gap-1.5">
          <input value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="Optional: a question the assistant would answer"
            className="flex-1 text-[11px] px-2 py-1.5 rounded-md border border-stone-300 bg-white" />
          <button onClick={explain} disabled={loadingCtx}
            className="text-[11px] px-2.5 py-1.5 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-100 disabled:opacity-50">
            {loadingCtx ? 'Explaining…' : 'Explain context'}
          </button>
        </div>

        {loadingCtx && (
          <p className="mt-2 flex items-center gap-1.5 text-stone-400 text-[11px]">
            <Loader2 size={12} className="animate-spin" /> Assembling context…</p>
        )}
        {!loadingCtx && ctx && (
          <div className="mt-2.5 space-y-2">
            <div className="flex items-center gap-2 flex-wrap text-[11px]">
              <span className={`text-[9px] px-1.5 py-0.5 rounded-full border capitalize ${(STATUS_CLS[ctx.status]) || ''}`}>
                context {ctx.status}
              </span>
              <span className="text-stone-500">
                evidence coverage:{' '}
                <b className="text-stone-800">
                  {ctx.evidence_coverage == null ? '—' : `${Math.round(ctx.evidence_coverage * 100)}%`}
                </b>
              </span>
              <span className="text-stone-500">
                budget: {ctx.token_budget?.included_items ?? '—'} included
                {ctx.token_budget?.omitted_items
                  ? ` · ${ctx.token_budget.omitted_items} omitted`
                  : ''}
              </span>
            </div>

            {(ctx.warnings || []).length > 0 && (
              <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-2 py-1">
                {ctx.warnings.map((w, i) => <div key={i}>{w}</div>)}
              </div>
            )}

            <ContextSection title="Project & resources">
              {ctx.project?.authorized ? (
                <div>
                  <span className="text-stone-700">{ctx.project.name}</span>
                  {(ctx.project.resources || []).length > 0 && (
                    <div className="mt-0.5 text-stone-500">
                      can reference: {(ctx.project.resources || [])
                        .map((r) => `${r.kind} “${r.name}”`).join(', ')}
                    </div>
                  )}
                </div>
              ) : ctx.project?.project_id ? (
                <span className="text-stone-500">
                  project {ctx.project.project_id} — {ctx.project.unavailable_reason || 'not authorized'}
                </span>
              ) : (
                <span className="text-stone-500">no project context selected</span>
              )}
              {ctx.department?.name && (
                <div className="mt-0.5 text-stone-500">
                  department {ctx.department.name} (context only — not an access grant)
                </div>
              )}
            </ContextSection>

            <ContextSection title="Business rules the assistant works from">
              {enabled.length === 0 && verified.length === 0 && proposed.length === 0 ? (
                <span className="text-stone-500">No rules matched this context.</span>
              ) : (
                <div className="space-y-1">
                  {enabled.map((r, i) => (
                    <div key={`e${i}`} className="text-emerald-700">
                      <b>executable:</b> {r.statement}
                    </div>
                  ))}
                  {verified.map((r, i) => (
                    <div key={`v${i}`} className="text-sky-700">
                      <b>verified (informational):</b> {r.statement}
                    </div>
                  ))}
                  {proposed.length > 0 && (
                    <div className="text-stone-500">
                      <b>hypotheses (not guidance):</b>{' '}
                      {proposed.map((r) => r.statement).join(' · ')}
                    </div>
                  )}
                </div>
              )}
            </ContextSection>

            {(ctx.ontology?.objects || []).length > 0 && (
              <ContextSection title="Business concepts in scope">
                <div className="flex flex-wrap gap-1">
                  {ctx.ontology.objects.map((o, i) => (
                    <span key={i} className="text-[10px] px-1.5 py-0.5 rounded-full border border-stone-200 bg-stone-50 text-stone-600">
                      {o.name}
                    </span>
                  ))}
                </div>
              </ContextSection>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
