/**
 * UITest — Smoke-test page for all Phase 3–7 UI components.
 *
 * Renders the new governed-AI components with mock data plus an interactive
 * form to verify rendering, state, and interactivity.  Also pings a few
 * backend endpoints to confirm the API responds.
 *
 * Route: /ui-test
 */

import { useState } from 'react';
import ArtifactPreviewCard from '@/components/chat/ArtifactPreviewCard';
import SandboxTimeline from '@/components/chat/SandboxTimeline';
import PlanEditor from '@/components/chat/PlanEditor';
import ActivityRail from '@/components/chat/ActivityRail';
import ReportCard from '@/components/chat/ReportCard';

/* ---------- mock data ---------- */
const MOCK_ARTIFACT = {
  id: 'test-art-001',
  title: 'Q3 Sales Report (Mock)',
  artifact_type: 'md',
  status: 'preview_ready',
  file_size: 24576,
  versions: [{ version_number: 2 }],
};

const MOCK_PLAN = {
  status: 'draft',
  is_acyclic: true,
  summary: 'Generate Q3 report from sales data, validate, and publish.',
  nodes: [
    { name: 'Query Sales DB', node_type: 'nl2sql', status: 'pending', description: 'Fetch Q3 sales records', expected_output: '12 rows' },
    { name: 'Build Report', node_type: 'sandbox', status: 'pending', description: 'Generate markdown report from query results' },
    { name: 'Validate Quality', node_type: 'skill', status: 'pending', description: 'Check report completeness' },
    { name: 'Publish', node_type: 'agent', status: 'pending', description: 'Send to stakeholders' },
  ],
};

const MOCK_EXECUTION = {
  current_state: 'act',
  observations: [
    { tool_name: 'nl2sql', success: true, duration_ms: 340, result_text: 'Returned 12 rows from sales table.' },
    { tool_name: 'sandbox', success: false, duration_ms: 5000, error_message: 'Container exceeded memory limit.' },
  ],
  confidence_score: null,
};

const MOCK_EXECUTION_DONE = {
  current_state: 'done',
  confidence_score: 0.87,
  confidence_factors: {
    output_validated: { score: 0.9 },
    policy_compliance: { score: 1.0 },
    error_rate: { score: 0.7 },
  },
  observations: MOCK_EXECUTION.observations,
};

/* ---------- ReportCard mock payload ---------- */
// A representative ReportCardPayload shaped exactly like the Pydantic
// model backend/app/services/synexia/contracts.py defines.  The
// ReportCard.jsx component reads this directly, so if the schema
// drifts, the renderer will show an empty card and the e2e test
// will fail.
const MOCK_REPORT_CARD = {
  title: 'Sales report — top materials by revenue',
  source: 'sales_orders · demo_db',
  generated_at: '2026-07-13T08:30:00Z',
  summary: 'Top 7 materials account for 76% of revenue; \u78b3\u4e94\u77f3\u6cb9\u6811\u8102 alone is 35%.',
  kpis: [
    { label: 'Total revenue',  value: '189.3M CNY', delta: '+12%',  caption: 'Top 7 materials' },
    { label: 'Total quantity', value: '11,210 tons', delta: '+4%', caption: 'All time' },
    { label: 'Top share',      value: '35%',         delta: null,  caption: '\u78b3\u4e94\u77f3\u6cb9\u6811\u8102' },
    { label: 'Row count',      value: '7',           delta: null,  caption: 'Distinct materials' },
  ],
  chart: {
    type: 'bar',
    title: 'Top materials by revenue',
    x_key: 'material_name',
    y_keys: ['total_revenue'],
    unit: 'CNY',
    data: [
      { material_name: '\u78b3\u4e94\u77f3\u6cb9\u6811\u8102', total_revenue: 66255000 },
      { material_name: 'Material B', total_revenue: 22100000 },
      { material_name: 'Material C', total_revenue: 18700000 },
      { material_name: 'Material D', total_revenue: 12500000 },
      { material_name: 'Material E', total_revenue: 8300000 },
    ],
  },
  insights: [
    { icon: 'trending_up',     text: 'Top 3 materials account for 76% of revenue — concentration risk.' },
    { icon: 'shield_alert',    text: '\u78b3\u4e94\u77f3\u6cb9\u6811\u8102 is 3x the next material.' },
    { icon: 'lightbulb',       text: 'Material D dropped 4% MoM despite a flat market — worth investigating.' },
  ],
  next_step: 'Want to break this down by region/month, or save this as a recurring weekly report?',
  actions: [
    { label: 'Break down by region', prompt: 'Break this down by region.' },
    { label: 'Save as weekly',       prompt: 'Save this as a recurring weekly report.' },
  ],
  user_signal: 'export',
  warnings: ['Snapshot was capped to 5 rows for the chart slide.'],
};

/* ---------- API probe ---------- */
async function probe(url) {
  const t0 = performance.now();
  try {
    const res = await fetch(url);
    const ms = Math.round(performance.now() - t0);
    // 200 or 404 both mean "server responded"; 5xx / network error = fail
    return { ok: res.ok, status: res.status, ms, error: null };
  } catch (e) {
    return { ok: false, status: 0, ms: Math.round(performance.now() - t0), error: e.message };
  }
}

/* ---------- the page ---------- */
export default function UITest() {
  const [planApproved, setPlanApproved] = useState(false);
  const [planRejected, setPlanRejected] = useState(false);
  const [execView, setExecView] = useState('running'); // running | done
  const [form, setForm] = useState({ name: '', priority: 'medium', notify: true, notes: '' });
  const [submitted, setSubmitted] = useState(null);
  const [apiResults, setApiResults] = useState(null);
  const [apiLoading, setApiLoading] = useState(false);

  const mockPlan = { ...MOCK_PLAN, status: planApproved ? 'approved' : planRejected ? 'rejected' : 'draft' };

  function handleSubmit(e) {
    e.preventDefault();
    setSubmitted({ ...form, at: new Date().toLocaleTimeString() });
  }

  async function runApiProbes() {
    setApiLoading(true);
    const endpoints = [
      { name: 'Governance Cost', url: '/api/governance/cost' },
      { name: 'Governance Audit', url: '/api/governance/audit?limit=5' },
      { name: 'Artifacts List', url: '/api/artifacts' },
      { name: 'Data Snapshots', url: '/api/data-snapshots' },
      { name: 'Agent Studio', url: '/api/agent-studio/test/preflight' },
      { name: 'Skill Studio', url: '/api/skill-studio/candidates' },
    ];
    const results = await Promise.all(
      endpoints.map(async (ep) => ({ name: ep.name, ...(await probe(ep.url)) }))
    );
    setApiResults(results);
    setApiLoading(false);
  }

  return (
    <div className="mx-auto max-w-5xl space-y-8 p-6">
      <header>
        <h1 className="text-2xl font-bold text-foreground">UI Component Test Suite</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Renders all Phase 3–7 governed-AI components with mock data + an interactive form.
        </p>
      </header>

      {/* 1. ArtifactPreviewCard */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          1. ArtifactPreviewCard
        </h2>
        <ArtifactPreviewCard artifact={MOCK_ARTIFACT} />
      </section>

      {/* 2. PlanEditor (interactive) */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          2. PlanEditor {planApproved && <span className="text-green-600">— Approved ✓</span>}
          {planRejected && <span className="text-red-500">— Rejected ✗</span>}
        </h2>
        <PlanEditor
          plan={mockPlan}
          onApprove={() => { setPlanApproved(true); setPlanRejected(false); }}
          onReject={() => { setPlanRejected(true); setPlanApproved(false); }}
        />
      </section>

      {/* 3. ActivityRail (toggle running/done) */}
      <section>
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            3. ActivityRail
          </h2>
          <div className="flex gap-1 rounded-lg border border-border p-0.5">
            <button
              onClick={() => setExecView('running')}
              className={`rounded-md px-3 py-1 text-xs font-medium ${execView === 'running' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}`}
            >Running</button>
            <button
              onClick={() => setExecView('done')}
              className={`rounded-md px-3 py-1 text-xs font-medium ${execView === 'done' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'}`}
            >Done</button>
          </div>
        </div>
        <ActivityRail execution={execView === 'running' ? MOCK_EXECUTION : MOCK_EXECUTION_DONE} />
      </section>

      {/* 4. SandboxTimeline (renders idle state) */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          4. SandboxTimeline (idle render)
        </h2>
        <SandboxTimeline jobId="mock-job-0001" />
      </section>

      {/* 4b. ReportCard (Synexia FSM report surface — Task 5/7 verification) */}
      <section data-testid="report-card-section">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          4b. ReportCard (Synexia FSM report)
        </h2>
        <p className="mb-3 text-xs text-muted-foreground">
          Renders the <code>report_card_payload</code> that the
          backend <code>finalize.py</code> attaches to assistant tool calls.
          Verifies KPI tiles, chart, insight bullets, and Export bar all
          render from a representative payload.
        </p>
        <div data-testid="report-card-wrapper">
          <ReportCard
            payload={MOCK_REPORT_CARD}
            artifactId="mock-artifact-001"
            userSignal="export"
            onAction={(prompt) => { window.alert('Mock action: ' + prompt); }}
          />
        </div>
      </section>

      {/* 5. Interactive Form */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          5. Interactive Form
        </h2>
        <form onSubmit={handleSubmit} className="max-w-md space-y-4 rounded-xl border border-border bg-card p-5">
          <div>
            <label className="mb-1 block text-xs font-medium text-foreground">Task Name</label>
            <input
              type="text"
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="e.g. Generate Q3 report"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-foreground">Priority</label>
            <select
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value })}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="notify"
              checked={form.notify}
              onChange={(e) => setForm({ ...form, notify: e.target.checked })}
              className="h-4 w-4 rounded border-border"
            />
            <label htmlFor="notify" className="text-sm text-foreground">Notify on completion</label>
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-foreground">Notes</label>
            <textarea
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
              rows={2}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
            />
          </div>
          <button
            type="submit"
            className="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          >
            Submit Task
          </button>
        </form>
        {submitted && (
          <div className="mt-3 rounded-lg border border-green-200 bg-green-50 p-3 text-sm">
            <p className="font-medium text-green-700">Form submitted at {submitted.at}</p>
            <pre className="mt-1 text-xs text-green-600">{JSON.stringify(submitted, null, 2)}</pre>
          </div>
        )}
      </section>

      {/* 6. API Response Probe */}
      <section>
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            6. Backend API Response Probe
          </h2>
          <button
            onClick={runApiProbes}
            disabled={apiLoading}
            className="rounded-md border border-border px-3 py-1 text-xs font-medium hover:bg-secondary disabled:opacity-50"
          >
            {apiLoading ? 'Probing...' : 'Run Probes'}
          </button>
        </div>
        {apiResults && (
          <div className="overflow-hidden rounded-xl border border-border">
            <table className="w-full text-sm">
              <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-2">Endpoint</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2">HTTP</th>
                  <th className="px-4 py-2">Latency</th>
                </tr>
              </thead>
              <tbody>
                {apiResults.map((r, i) => (
                  <tr key={i} className="border-t border-border">
                    <td className="px-4 py-2 font-medium text-foreground">{r.name}</td>
                    <td className="px-4 py-2">
                      {r.error ? (
                        <span className="text-red-500">ERROR</span>
                      ) : r.ok ? (
                        <span className="text-green-600">OK</span>
                      ) : (
                        <span className="text-amber-500">RESPONDED</span>
                      )}
                    </td>
                    <td className="px-4 py-2 font-mono text-muted-foreground">{r.status || '—'}</td>
                    <td className="px-4 py-2 font-mono text-muted-foreground">{r.ms}ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {apiResults && (
          <p className="mt-2 text-xs text-muted-foreground">
            Note: HTTP 200 = data returned. 404/422 = endpoint exists but no data / bad params.
            Network error = backend unreachable.
          </p>
        )}
      </section>

      <footer className="border-t border-border pt-4 text-center text-xs text-muted-foreground">
        UITest page — remove after verification.
      </footer>
    </div>
  );
}
