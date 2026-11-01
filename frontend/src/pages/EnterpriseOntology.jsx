import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { authFetch } from '@/api/authFetch';
import { useAuth } from '@/lib/AuthContext';
import OntologyOverview from '@/components/ontology/OntologyOverview';
import OntologyGraphPanel from '@/components/ontology/OntologyGraphPanel';
import BusinessLogicPanel from '@/components/ontology/BusinessLogicPanel';
import WorkforcePanel from '@/components/enterprise/WorkforcePanel';
import GovernanceDrawer from '@/components/ontology/GovernanceDrawer';

/* Enterprise Ontology — ONE page, four sections.

   Core principle shown on the overview:
     Enterprise Data → Enterprise Ontology → AI Intelligence → Business
     Decisions.

   Progressive disclosure: the overview answers "how does this work" in ten
   seconds; deeper inspection (object drawer, relationships, rules,
   agent context) happens in place. All data comes from the existing
   authorized ontology/BCS APIs — nothing is recomputed client-side and
   no value is fabricated. Administrative operations remain in the
   existing administration area; this page is read-only. */

const SECTIONS = ['Overview', 'Business Model', 'Business Logic', 'AI Intelligence'];
const VIEW_KEYS = { Overview: 'overview', 'Business Model': 'model',
                    'Business Logic': 'logic', 'AI Intelligence': 'ai' };

const LAYER_TO_SECTION = { data: 'overview', ontology: 'model', ai: 'ai', decisions: 'ai' };

export default function EnterpriseOntology() {
  const { isAdmin } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get('view');
  const view = Object.values(VIEW_KEYS).includes(raw) ? raw : 'overview';

  const [overview, setOverview] = useState(null); // null loading, false failed
  const [systems, setSystems] = useState(null);   // null unavailable
  const [objects, setObjects] = useState(null);
  const [links, setLinks] = useState(null);
  const [modelFailed, setModelFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  // Phase 7A: governance — admins get a pending-review count on the
  // overview and a drawer that reuses the existing ReviewCenter.
  const [pendingReviews, setPendingReviews] = useState(null);
  const [showGovernance, setShowGovernance] = useState(false);

  useEffect(() => {
    if (!isAdmin) return; // admin-only endpoint — never called for members
    let alive = true;
    (async () => {
      try {
        const res = await authFetch('/api/ontology/enterprise/review-queue');
        if (!res.ok) { if (alive) setPendingReviews(null); return; }
        const q = await res.json();
        if (alive) setPendingReviews(Object.values(q.counts || {})
          .reduce((a, b) => a + b, 0));
      } catch { if (alive) setPendingReviews(null); }
    })();
    return () => { alive = false; };
  }, [isAdmin, showGovernance]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    (async () => {
      try {
        const [ov, mp] = await Promise.all([
          authFetch('/api/ontology/overview'),
          authFetch('/api/ontology/enterprise/map'),
        ]);
        if (!alive) return;
        setOverview(ov.ok ? await ov.json() : false);
        setSystems(mp.ok ? (await mp.json()).systems || [] : null);
      } catch {
        if (alive) { setOverview(false); setSystems(null); }
      } finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; };
  }, []);

  // model data loads with the page (used by section B; cheap, parallel)
  useEffect(() => {
    if (view !== 'model') return;
    let alive = true;
    (async () => {
      try {
        const [ob, lb] = await Promise.all([
          authFetch('/api/ontology/objects'),
          authFetch('/api/ontology/links'),
        ]);
        if (!alive) return;
        if (!ob.ok || !lb.ok) { setModelFailed(true); return; }
        // resolve BOTH bodies before setting state: two awaited setState
        // calls would flush an intermediate render with objects set and
        // links still null (the panel crashed on that before its guard)
        const objs = await ob.json();
        const lns = await lb.json();
        if (!alive) return;
        setObjects(objs);
        setLinks(lns);
        setModelFailed(false);
      } catch {
        if (alive) setModelFailed(true);
      }
    })();
    return () => { alive = false; };
  }, [view]);

  const setView = (label) =>
    setSearchParams(
      VIEW_KEYS[label] === 'overview' ? '' : `view=${VIEW_KEYS[label]}`,
      { replace: false });

  return (
    <div className="min-h-screen bg-white">
      <div className="mx-auto max-w-6xl px-6 py-8">
        <header className="mb-6">
          <h1 className="text-[17px] font-semibold text-stone-900">Enterprise Ontology</h1>
          <p className="mt-1 text-[12px] text-stone-500">
            How enterprise data becomes governed business knowledge and AI-assisted decisions
          </p>
        </header>

        <nav className="mb-6 flex items-center gap-1 border-b border-stone-100 pb-2" aria-label="Ontology sections">
          {SECTIONS.map((label) => {
            const active = view === VIEW_KEYS[label];
            return (
              <button key={label} onClick={() => setView(label)}
                aria-current={active ? 'page' : undefined}
                className={`rounded-md px-3 py-1.5 text-[13px] transition-colors ${
                  active
                    ? 'bg-stone-100 font-medium text-stone-900'
                    : 'text-stone-500 hover:text-stone-800'}`}>
                {label}
              </button>
            );
          })}
        </nav>

        {loading && view === 'overview' ? (
          <p className="text-[12px] text-stone-400">Loading…</p>
        ) : (
          <main>
            {view === 'overview' && (
              <>
                <OntologyOverview overview={overview} systems={systems}
                  onLayerClick={(layer) => setView(
                    Object.keys(VIEW_KEYS).find(
                      (k) => VIEW_KEYS[k] === LAYER_TO_SECTION[layer]))} />
                {/* Phase 7A: governance entry — admins only; the queue API
                    is org-admin gated server-side, this only avoids noise */}
                {isAdmin && pendingReviews != null && (
                  <div className="mt-4 rounded-xl border border-stone-200 bg-stone-50 px-4 py-3 flex items-center gap-3">
                    <span className="text-[12px] text-stone-700">
                      <b>{pendingReviews}</b> AI discovery proposal(s)&nbsp;
                      <span className="text-stone-500">Need human review</span>
                    </span>
                    <button onClick={() => setShowGovernance(true)}
                      className="ml-auto text-[11px] px-2.5 py-1.5 rounded-md border border-stone-300 bg-white text-stone-700 hover:bg-stone-100">
                      Review now
                    </button>
                  </div>
                )}
              </>
            )}
            {view === 'model' && (
              <OntologyGraphPanel overview={overview} objects={objects}
                links={links} failed={modelFailed} />
            )}
            {view === 'logic' && <BusinessLogicPanel />}
            {view === 'ai' && (
              <>
                {/* Phase 7A: lifecycle transparency — the exact contract the
                    existing BCS enforces (enabled=executable, verified=
                    informational, proposed=hypotheses, rejected excluded) */}
                <div className="mb-4 rounded-xl border border-stone-200 bg-white px-4 py-3 text-[11px] text-stone-600">
                  <h3 className="text-[12px] font-medium text-stone-800">
                    What can influence AI answers
                  </h3>
                  <p className="mt-1">
                    AI assistants work from <b>verified facts and enabled rules</b>.
                    Discovered-but-unreviewed knowledge appears only as clearly
                    labeled <b>hypotheses</b> (non-binding, limited in count).
                    Rejected or disabled knowledge is <b>never included</b> in
                    assistant context.
                  </p>
                </div>
                <WorkforcePanel />
              </>
            )}
          </main>
        )}
        {showGovernance && (
          <GovernanceDrawer onClose={() => setShowGovernance(false)} />
        )}
      </div>
    </div>
  );
}
