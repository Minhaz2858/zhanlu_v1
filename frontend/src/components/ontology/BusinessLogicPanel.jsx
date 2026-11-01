import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { authFetch } from '@/api/authFetch';

/* Section C — Business Logic: the essential rules and actions, read-only.
   Detailed lifecycle management stays in the administration area
   (/admin/enterprise-profile → Review). Only the marker-protocol effect is
   surfaced, and only for ENABLED rules — proposed knowledge is never
   presented as enforcement. */

function ruleEffect(statement) {
  const head = (statement || '').trim().toLowerCase();
  if (head.startsWith('prohibit:') || head.startsWith('forbidden:') ||
      head.startsWith('deny:')) return 'blocks';
  if (head.startsWith('require_approval:')) return 'needs approval';
  return 'informational';
}

export default function BusinessLogicPanel() {
  const [rules, setRules] = useState(null);
  const [actions, setActions] = useState(null);
  const [objects, setObjects] = useState(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [r, a, o] = await Promise.all([
          authFetch('/api/ontology/enterprise/business-rules?page_size=50'),
          authFetch('/api/ontology/actions'),
          authFetch('/api/ontology/objects'),
        ]);
        if (!r.ok || !a.ok || !o.ok) throw new Error('load failed');
        const [rb, ab, ob] = await Promise.all([r.json(), a.json(), o.json()]);
        if (!alive) return;
        setRules(Array.isArray(rb) ? rb : (rb.items || []));
        setActions(Array.isArray(ab) ? ab : []);
        setObjects(Array.isArray(ob) ? ob : []);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => { alive = false; };
  }, []);

  if (failed) {
    return <p className="text-[12px] text-stone-400">Business logic is not available right now.</p>;
  }
  if (rules == null) {
    return <p className="flex items-center gap-1.5 text-[12px] text-stone-400">
      <Loader2 size={13} className="animate-spin" /> Loading business logic…</p>;
  }

  const objName = (id) => objects?.find((o) => o.id === id)?.name || id;
  const hasContent = rules.length > 0 || (actions || []).length > 0;

  return (
    <div className="space-y-4">
      {!hasContent && (
        <p className="rounded-xl border border-dashed border-stone-300 bg-stone-50 px-4 py-3 text-[12px] text-stone-500">
          No business rules or actions recorded yet — they appear here once
          knowledge is discovered and reviewed.
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
          <h3 className="text-[12px] font-medium text-stone-800">Business rules</h3>
          {rules.length === 0 ? (
            <p className="mt-1 text-[11px] text-stone-400">No rules recorded yet.</p>
          ) : (
            <div className="mt-2 divide-y divide-stone-100">
              {rules.map((r) => {
                const effect = ruleEffect(r.statement);
                const active = r.status === 'enabled' && effect !== 'informational';
                return (
                  <div key={r.id} className="py-2">
                    <div className="text-[12px] text-stone-800">{r.statement}</div>
                    <div className="mt-0.5 flex items-center gap-2 text-[10px] text-stone-500">
                      <span className={`capitalize ${r.status === 'enabled' ? 'text-emerald-600' : ''}`}>
                        {r.status}
                      </span>
                      {active && <span className="text-stone-400">· {effect}</span>}
                      {r.scope_ref_id && (
                        <span className="text-stone-400">· {objName(r.scope_ref_id)}</span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          <p className="mt-2 text-[10px] text-stone-400">
            Rule lifecycle changes (enable / disable) are restricted to
            authorized administrators.
          </p>
        </div>

        <div className="rounded-xl border border-stone-200 bg-white px-4 py-3">
          <h3 className="text-[12px] font-medium text-stone-800">Business actions</h3>
          {(actions || []).length === 0 ? (
            <p className="mt-1 text-[11px] text-stone-400">No actions recorded yet.</p>
          ) : (
            <div className="mt-2 divide-y divide-stone-100">
              {actions.map((a) => (
                <div key={a.id} className="py-2">
                  <div className="text-[12px] text-stone-800">{a.name}</div>
                  <div className="mt-0.5 flex items-center gap-2 text-[10px] text-stone-500">
                    {a.description && <span>{a.description}</span>}
                    <span className={a.requires_approval ? 'text-amber-600' : 'text-stone-400'}>
                      {a.requires_approval ? '· approval required' : '· no approval needed'}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
