import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { authFetch } from '@/api/authFetch';
import OntologyOverview from '@/components/ontology/OntologyOverview';
import OntologyGraphPanel from '@/components/ontology/OntologyGraphPanel';
import BusinessLogicPanel from '@/components/ontology/BusinessLogicPanel';
import WorkforcePanel from '@/components/enterprise/WorkforcePanel';

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
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get('view');
  const view = Object.values(VIEW_KEYS).includes(raw) ? raw : 'overview';

  const [overview, setOverview] = useState(null); // null loading, false failed
  const [systems, setSystems] = useState(null);   // null unavailable
  const [objects, setObjects] = useState(null);
  const [links, setLinks] = useState(null);
  const [modelFailed, setModelFailed] = useState(false);
  const [loading, setLoading] = useState(true);

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
              <OntologyOverview overview={overview} systems={systems}
                onLayerClick={(layer) => setView(
                  Object.keys(VIEW_KEYS).find(
                    (k) => VIEW_KEYS[k] === LAYER_TO_SECTION[layer]))} />
            )}
            {view === 'model' && (
              <OntologyGraphPanel overview={overview} objects={objects}
                links={links} failed={modelFailed} />
            )}
            {view === 'logic' && <BusinessLogicPanel />}
            {view === 'ai' && <WorkforcePanel />}
          </main>
        )}
      </div>
    </div>
  );
}
