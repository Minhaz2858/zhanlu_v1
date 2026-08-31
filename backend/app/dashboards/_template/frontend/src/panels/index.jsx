/**
 * Typed AI-analysis panels — the decision-center information architecture.
 *
 * These are the presentation components that make an executive dashboard
 * professional (the Ecisco CEO Command Center patterns): a severity-rail
 * alert strip where every alert is "data → why it matters → recommended
 * action", approval decision cards with quantified P&L impact, cost/value
 * cascade chains, account-health rows, inventory coverage bars, competitor
 * pricing-position bands, an activity feed, and long-form AI analysis blocks.
 *
 * Every panel is AI-authored from REAL queried data (the agent computes
 * figures via execute_query / metric deltas, then narrates). The renderer
 * never invents anything — it only styles what the spec declares.
 *
 * Styling is class-driven (.dc-*) and token-driven (CSS variables), so the
 * panels adapt to every style (standard / chinese_bi / ceo / editorial).
 */
import { useMemo } from 'react';

/* Tone → CSS class. Base semantics (standard / ceo / editorial):
 *   up=good(green), down=bad(red), warn=amber, neutral=muted.
 * .chinese-bi swaps up/down colors in CSS (China: red=up, green=down). */
const TONE_CLS = {
  up: 'tone-up',
  down: 'tone-down',
  warn: 'tone-warn',
  neutral: 'tone-neutral',
  good: 'tone-good',
  bad: 'tone-bad',
};

const SEVERITY_CLS = {
  crit: 'sev-crit',
  warn: 'sev-warn',
  opp: 'sev-opp',
  info: 'sev-info',
};

const SEVERITY_ICON = {
  crit: '⚠',
  warn: '▲',
  opp: '◆',
  info: 'ℹ',
};

const COMP_COLORS = ['#3B82F6', '#8B5CF6', '#14B8A6', '#F59E0B', '#EC4899', '#06B6D4'];

function cls(tone) {
  return TONE_CLS[tone] || 'tone-neutral';
}

/* ── Alert strip ─────────────────────────────────────────────────────── */
function AlertStrip({ panel }) {
  const items = panel.items || [];
  return (
    <div className="dc-strip">
      {items.map((a, i) => {
        const sev = SEVERITY_CLS[a.severity] || SEVERITY_CLS.info;
        return (
          <div key={i} className={`dc-alert ${sev}`}>
            <div className="dc-alert-icon" aria-hidden="true">
              {a.icon || SEVERITY_ICON[a.severity] || 'ℹ'}
            </div>
            <div className="dc-alert-body">
              <div className="dc-alert-title">{a.title}</div>
              {a.body ? <div className="dc-alert-desc">{a.body}</div> : null}
            </div>
            {a.cta ? <span className="dc-alert-cta">{a.cta}</span> : null}
            {a.time ? <span className="dc-alert-time">{a.time}</span> : null}
          </div>
        );
      })}
    </div>
  );
}

/* ── Decision cards (approval workflow) ──────────────────────────────── */
function DecisionCards({ panel }) {
  const items = panel.items || [];
  return (
    <div className="dc-dec-stack">
      {items.map((d, i) => {
        const tone = d.tag_tone || d.pnl_tone || 'neutral';
        const toneCls = TONE_CLS[tone] === 'tone-up' || TONE_CLS[tone] === 'tone-good'
          ? 'dec-pos'
          : TONE_CLS[tone] === 'tone-down' || TONE_CLS[tone] === 'tone-bad'
            ? 'dec-neg'
            : 'dec-warn';
        return (
          <div key={i} className={`dc-dec ${toneCls}`}>
            {d.tag ? (
              <span className={`dc-dec-tag ${cls(tone)}`}>{d.tag}</span>
            ) : null}
            {d.title ? <div className="dc-dec-prod">{d.title}</div> : null}
            {d.action ? (
              <div className={`dc-dec-action ${cls(d.action_tone || 'neutral')}`}>
                {d.action}
              </div>
            ) : null}
            {d.body ? <div className="dc-dec-body">{d.body}</div> : null}
            {d.pnl ? (
              <span className={`dc-dec-pnl ${cls(d.pnl_tone || 'neutral')}`}>{d.pnl}</span>
            ) : null}
            {Array.isArray(d.buttons) && d.buttons.length ? (
              <div className="dc-dec-btns">
                {d.buttons.map((b, bi) => (
                  <button key={bi} type="button" className={bi === 0 ? 'dbtn-a' : 'dbtn-d'} disabled>
                    {b}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

/* ── Long-form AI analysis narrative ─────────────────────────────────── */
function NarrativePanel({ panel }) {
  return (
    <div className="dc-narrative">
      <div className="dc-narrative-head">
        <span className="dc-ai-badge">✦ AI</span>
        {panel.title ? <span className="dc-narrative-title">{panel.title}</span> : null}
      </div>
      <div className="dc-narrative-body">{panel.body}</div>
    </div>
  );
}

/* ── Cost / value cascade chain ──────────────────────────────────────── */
function ChainPanel({ panel }) {
  const nodes = panel.nodes || [];
  return (
    <div>
      {panel.title ? <div className="dc-panel-title">{panel.title}</div> : null}
      <div className="dc-chain">
        {nodes.map((n, i) => (
          <div key={i} className="dc-chain-wrap">
            {i > 0 ? <span className="dc-chain-arrow" aria-hidden="true">→</span> : null}
            <div className="dc-chain-node">
              <div className="dc-chain-lbl">{n.label}</div>
              <div className="dc-chain-val">
                {n.value}
                {n.unit ? <span className="dc-chain-unit"> {n.unit}</span> : null}
              </div>
              {n.delta ? (
                <div className={`dc-chain-delta ${cls(n.delta_tone || 'neutral')}`}>{n.delta}</div>
              ) : null}
              {n.note ? (
                <div className={`dc-chain-note ${cls(n.note_tone || 'neutral')}`}>{n.note}</div>
              ) : null}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── Account-health rows ─────────────────────────────────────────────── */
function CustomersPanel({ panel }) {
  const rows = panel.rows || panel.items || [];
  return (
    <div className="dc-customers">
      {rows.map((c, i) => (
        <div key={i} className="dc-cust-row">
          <div className="dc-cust-av">{c.avatar || (c.name || '?').slice(0, 2)}</div>
          <div className="dc-cust-main">
            <div className="dc-cust-name">{c.name}</div>
            {c.sub ? <div className="dc-cust-sub">{c.sub}</div> : null}
          </div>
          <div className="dc-cust-side">
            {c.revenue ? <div className="dc-cust-rev">{c.revenue}</div> : null}
            {c.status ? (
              <div className={`dc-cust-status ${cls(c.status_tone || 'neutral')}`}>{c.status}</div>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── Inventory coverage bars ─────────────────────────────────────────── */
function InventoryPanel({ panel }) {
  const rows = panel.rows || panel.items || [];
  const max = panel.max || 8;
  return (
    <div className="dc-inv">
      {rows.map((r, i) => {
        const pct = Math.max(2, Math.min(100, ((r.weeks || 0) / max) * 100));
        return (
          <div key={i} className="dc-inv-row">
            <span className="dc-inv-lbl">{r.label}</span>
            <div className="dc-inv-track">
              <div className={`dc-inv-fill ${cls(r.tone || 'neutral')}`} style={{ width: `${pct}%` }} />
            </div>
            <span className={`dc-inv-val ${cls(r.tone || 'neutral')}`}>
              {typeof r.weeks === 'number' ? `${r.weeks.toFixed(1)}w` : r.weeks}
            </span>
            {r.status ? <span className={`dc-inv-status ${cls(r.tone || 'neutral')}`}>{r.status}</span> : null}
          </div>
        );
      })}
    </div>
  );
}

/* ── Competitor pricing-position bands ───────────────────────────────── */
function CompetitorsPanel({ panel }) {
  const rows = panel.rows || panel.items || [];
  return (
    <div className="dc-comp">
      {panel.title ? <div className="dc-panel-title">{panel.title}</div> : null}
      {rows.map((d, i) => {
        const lo = Number(d.lo ?? 0);
        const hi = Number(d.hi ?? 1);
        const rng = hi - lo || 1;
        const pos = (v) => Math.max(2, Math.min(98, ((Number(v) - lo) / rng) * 80 + 10));
        const ours = pos(d.our_price);
        const diffCls = d.diff_tone || (Number(d.diff) > 1.5 ? 'down' : Number(d.diff) < -1 ? 'up' : 'warn');
        return (
          <div key={i} className="dc-comp-row">
            <span className="dc-comp-name">{d.name}</span>
            <div className="dc-comp-track">
              <div className="dc-comp-range" />
              {(d.comps || []).map((c, ci) => (
                <span
                  key={ci}
                  className="dc-comp-dot"
                  title={c.name}
                  style={{ left: `${pos(c.price)}%`, background: COMP_COLORS[ci % COMP_COLORS.length] }}
                />
              ))}
              <span className="dc-comp-pin" style={{ left: `${ours}%` }} />
            </div>
            <span className="dc-comp-val">¥{Number(d.our_price).toLocaleString()}</span>
            <span className={`dc-comp-diff ${cls(diffCls)}`}>
              {(Number(d.diff) >= 0 ? '+' : '') + Number(d.diff).toFixed(1)}%
            </span>
          </div>
        );
      })}
    </div>
  );
}

/* ── Activity / competitor news feed ─────────────────────────────────── */
function NewsPanel({ panel }) {
  const rows = panel.rows || panel.items || [];
  return (
    <div className="dc-news">
      {rows.map((n, i) => (
        <div key={i} className="dc-news-row">
          {n.badge ? <span className={`dc-news-badge ${cls(n.badge_tone || 'neutral')}`}>{n.badge}</span> : null}
          <span className="dc-news-text">{n.text}</span>
          {n.time ? <span className="dc-news-time">{n.time}</span> : null}
        </div>
      ))}
    </div>
  );
}

/* ── Panel dispatcher ────────────────────────────────────────────────── */
const RENDERERS = {
  alerts: AlertStrip,
  decisions: DecisionCards,
  narrative: NarrativePanel,
  chain: ChainPanel,
  customers: CustomersPanel,
  inventory: InventoryPanel,
  competitors: CompetitorsPanel,
  news: NewsPanel,
};

export function Panel({ panel }) {
  const Comp = RENDERERS[panel?.type] || null;
  if (!Comp) return null;
  const span = panel?.span || 'full';
  return (
    <section className={`dc-panel dc-span-${span}`} aria-label={panel?.title || panel?.type}>
      {panel?.title && !['narrative', 'chain', 'competitors'].includes(panel?.type) ? (
        <div className="dc-panel-title">{panel.title}</div>
      ) : null}
      <Comp panel={panel} />
    </section>
  );
}

/** Group panels by page id (panels without a page go to the first page). */
export function panelsForPage(panels, pageId) {
  return (panels || []).filter((p) => (p.page || 'overview') === pageId);
}
