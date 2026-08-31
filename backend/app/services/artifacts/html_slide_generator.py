"""Render individual slides as HTML using the theme catalog.

One function per layout.  Returns a single ``<section class="slide
slide--{layout}">...</section>`` string.  The wrapping ``index.html``
that the renderer builds (Task 7) handles the full-page stage,
viewport-base.css inclusion, and JS bundle.
"""
from __future__ import annotations

import html
import re as _re
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.artifacts.deck_hero import hero_background_css
from app.services.artifacts.themes import ThemePreset
from app.services.synexia.contracts import SlidePlan

# Vendored Chart.js (backend/app/static/vendor/chart.umd.min.js) — inlined
# into slide HTML so decks never depend on an external CDN at render time.
# Falls back to the CDN URL only when the vendored file is missing.
_VENDORED_CHART_JS: str = ""


def _load_vendored_chart_js() -> str:
    """Return the vendored Chart.js source (cached), or "" if unavailable."""
    global _VENDORED_CHART_JS
    if _VENDORED_CHART_JS:
        return _VENDORED_CHART_JS
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parent.parent.parent / "static" / "vendor" / "chart.umd.min.js",
        Path("/app/static/vendor/chart.umd.min.js"),
    ]
    for p in candidates:
        try:
            if p.exists():
                _VENDORED_CHART_JS = p.read_text(encoding="utf-8")
                return _VENDORED_CHART_JS
        except OSError:
            continue
    return ""


def _chart_loader_tag() -> str:
    """Script tag that provides the Chart global.

    Inline vendored source when available (no network, deterministic);
    otherwise fall back to the jsdelivr CDN URL.
    """
    src = _load_vendored_chart_js()
    if src:
        return f"<script>{src}</script>"
    return '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>'


def _theme_css(theme: ThemePreset) -> str:
    """Build a ``:root { --token: value; ... }`` block from the theme's tokens.

    Emits BOTH underscore and hyphenated forms (``--bg_primary`` AND
    ``--bg-primary``) so layout CSS can use either naming convention.
    (Historically the token dict used underscores while layout CSS used
    hyphens — emitting both aliases fixes the mismatch.)
    """
    lines = [":root {"]
    for k, v in theme.color_tokens.items():
        lines.append(f"  --{k}: {v};")
        if "_" in k:
            lines.append(f"  --{k.replace('_', '-')}: {v};")
    lines.append(f"  --font-display: '{theme.font_display}', serif;")
    lines.append(f"  --font-body: '{theme.font_body}', sans-serif;")
    lines.append("}")
    return "\n".join(lines)


# --- per-layout renderers ----------------------------------------------------

def _render_cover(plan: SlidePlan, theme: ThemePreset) -> str:
    title = html.escape(plan.title or "")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    css = _theme_css(theme)
    subtitle_html = f'<p class="cover__subtitle">{subtitle}</p>' if subtitle else ""
    # Professional cover furniture: period + date + brand line (Kimi /
    # Claude-grade covers always carry a context strip under the title).
    meta_bits: List[str] = []
    if plan.period:
        meta_bits.append(html.escape(plan.period))
    from datetime import date as _date
    meta_bits.append(_date.today().strftime("%B %Y"))
    meta_bits.append("SYNEXIA")
    meta_html = (
        f'<p class="cover__meta">{"&nbsp;·&nbsp;".join(meta_bits)}</p>'
        if meta_bits else ""
    )
    # Hero art: explicit AI image wins; else deterministic theme-aware SVG
    # (settings-gated, always safe). Seed on title so covers vary per deck.
    hero_css = ""
    if settings.DECK_HERO_ART_ENABLED:
        if plan.hero_image:
            hero_css = (
                f"background-image: url('{html.escape(plan.hero_image, quote=True)}'), "
                f"linear-gradient(180deg, rgba(0,0,0,0) 55%, rgba(0,0,0,0.35) 100%);"
            )
        else:
            hero_css = hero_background_css(theme, plan.title or "cover", "cover")
    return f"""<style>
{css}
/* === COVER LAYOUT === */
.slide--cover {{
  width: 1920px; height: 1080px;
  background: var(--bg-primary, #1a1a1a);
  background-size: cover;
  {hero_css}
  color: var(--text-primary, #fff);
  font-family: var(--font-body);
  display: grid; grid-template-columns: 1fr; align-content: end;
  padding: 96px; box-sizing: border-box;
}}
.cover__title {{
  font-family: var(--font-display); font-weight: 900;
  font-size: 128px; line-height: 1.05; letter-spacing: -0.02em;
  margin: 0 0 24px 0;
}}
.cover__subtitle {{ font-size: 36px; font-weight: 400; opacity: 0.85; margin: 0 0 32px 0; }}
.cover__meta {{ font-size: 26px; font-weight: 500; opacity: 0.7;
  letter-spacing: 0.06em; text-transform: uppercase; margin: 0; }}
</style>
<section class="slide slide--cover">
  <h1 class="cover__title">{title}</h1>
  {subtitle_html}
  {meta_html}
</section>"""


def _render_kpi_grid(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Key Metrics")
    tiles: List[str] = []
    for spec in (plan.kpi_specs or []):
        label = html.escape(spec.label or "")
        value = html.escape(spec.value or "")
        delta = html.escape(spec.delta or "") if spec.delta else ""
        caption = html.escape(spec.caption or "") if spec.caption else ""
        delta_html = f'<p class="kpi__delta">{delta}</p>' if delta else ""
        caption_html = f'<p class="kpi__caption">{caption}</p>' if caption else ""
        tiles.append(
            f'<div class="kpi">'
            f'<p class="kpi__label">{label}</p>'
            f'<p class="kpi__value">{value}</p>'
            f'{delta_html}{caption_html}'
            f'</div>'
        )
    tiles_html = "\n".join(tiles) if tiles else '<p class="kpi-empty">No KPIs</p>'
    return f"""<style>
{css}
/* === KPI GRID LAYOUT === */
.slide--kpi-grid {{
  width: 1920px; height: 1080px; background: var(--bg-primary, #fff);
  color: var(--text-primary, #0a0a0a); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-rows: auto 1fr; gap: 64px;
}}
.kpi-grid__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0; line-height: 1.1; }}
.kpi-grid__tiles {{ display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 48px; align-content: center; }}
.kpi {{ background: rgba(0,0,0,0.04); padding: 48px;
  border-radius: 16px; border-left: 8px solid var(--accent, #4361ee); }}
.kpi__label {{ font-size: 24px; text-transform: uppercase;
  letter-spacing: 0.08em; margin: 0 0 16px 0; opacity: 0.7; }}
.kpi__value {{ font-family: var(--font-display); font-weight: 800;
  font-size: 96px; line-height: 1; margin: 0 0 16px 0; }}
.kpi__delta {{ font-size: 28px; color: var(--accent, #4361ee);
  font-weight: 600; margin: 0 0 8px 0; }}
.kpi__caption {{ font-size: 18px; opacity: 0.6; margin: 0; }}
.kpi-empty {{ font-size: 32px; opacity: 0.4; text-align: center; }}
</style>
<section class="slide slide--kpi-grid">
  <h2 class="kpi-grid__title">{title}</h2>
  <div class="kpi-grid__tiles">{tiles_html}</div>
</section>"""


def _chart_data_from_plan(plan: SlidePlan) -> tuple:
    """Extract (chart_type, labels, values, series_labels) from a SlidePlan.

    Prefers ``plan.chart_rows`` (already materialized dicts); falls back
    to building labels from row count when only ``chart_spec`` is set.

    Multi-series: when a row has a label key plus 2+ numeric value keys,
    ``values`` becomes a list of per-row lists (one entry per series) and
    ``series_labels`` carries the numeric column names.
    """
    chart_type = "bar"
    if plan.chart_spec and plan.chart_spec.chart_type:
        chart_type = plan.chart_spec.chart_type
    labels: List[str] = []
    values: List = []
    series_labels: List[str] = []
    rows = plan.chart_rows or []
    # Detect multi-series: label key + >=2 numeric keys
    if rows:
        first = rows[0]
        keys = list(first.keys())
        if "label" in first:
            numeric_keys = [
                k for k in keys
                if k != "label" and _is_number(first.get(k))
            ]
        else:
            numeric_keys = [k for k in keys if _is_number(first.get(k))]
        if len(numeric_keys) >= 2:
            series_labels = [str(k) for k in numeric_keys]
            for row in rows:
                labels.append(str(row.get("label", row.get(keys[0], ""))))
                values.append([
                    _to_float(row.get(k, 0)) for k in numeric_keys
                ])
            return chart_type, labels, values, series_labels
    for row in rows:
        # ``label`` and ``value`` are the conventional keys; fall back
        # to the first two keys if those are absent.
        keys = list(row.keys())
        if not keys:
            continue
        label_key = "label" if "label" in row else keys[0]
        value_key = "value" if "value" in row else (keys[1] if len(keys) > 1 else keys[0])
        labels.append(str(row.get(label_key, "")))
        try:
            values.append(float(row.get(value_key, 0)))
        except (TypeError, ValueError):
            values.append(0)
    return chart_type, labels, values, series_labels


def _is_number(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _chart_js(
    plan: SlidePlan,
    theme: ThemePreset,
    chart_id: str,
    labels: List,
    values: List,
    chart_type: str,
    series_labels: Optional[List[str]] = None,
) -> str:
    """Professional Chart.js config — the "Kimi-grade chart" upgrade.

    Adds what the old single-color chart lacked: a themed multi-series
    palette (``chart_series`` from the theme tokens), legend, gridlines,
    axis tick labels, and tooltips.  Colors are read from the rendered
    ``:root`` tokens at runtime, so the chart always matches the deck's
    palette even when the theme was resolved late.
    """
    series = list(getattr(theme, "color_tokens", {}).get("chart_series") or [])
    accent = str(getattr(theme, "color_tokens", {}).get("accent") or "#4361ee")
    palette = [str(c) for c in series] if len(series) >= 2 else [accent, "#94a3b8"]
    multi = bool(values) and isinstance(values[0], (list, tuple))
    if multi:
        n_series = len(values[0])
        datasets = [
            {
                "label": (series_labels or [f"Series {i+1}"])[i] if (series_labels and i < len(series_labels)) else f"Series {i+1}",
                "data": [float(r[i]) for r in values],
                "backgroundColor": palette[i % len(palette)],
                "borderRadius": 6,
                "borderSkipped": False,
            }
            for i in range(n_series)
        ]
        labels_for_js = [str(l) for l in labels]
    else:
        datasets = [
            {
                "data": [float(v) for v in values],
                "backgroundColor": palette,
                "borderRadius": 6,
                "borderSkipped": False,
            }
        ]
        labels_for_js = [str(l) for l in labels]
    chart_type = chart_type or "bar"
    if chart_type in ("column", "grouped_column", "stacked_column"):
        chart_type = "bar"
    if chart_type in ("grouped_bar", "stacked_bar", "combo"):
        chart_type = "bar"
    if chart_type in ("area",):
        chart_type = "line"
    show_legend = multi and n_series > 1
    import json as _json
    return f"""{_chart_loader_tag()}
<script>
  window.addEventListener('load', function() {{
    const root = getComputedStyle(document.documentElement);
    const txt = root.getPropertyValue('--text-primary').trim() || '#0a0a0a';
    const grid = root.getPropertyValue('--muted').trim() || 'rgba(0,0,0,0.1)';
    const ctx = document.getElementById('{chart_id}');
    if (!ctx || typeof Chart === 'undefined') {{ return; }}
    new Chart(ctx, {{
      type: '{chart_type}',
      data: {{
        labels: {_json.dumps(labels_for_js)},
        datasets: {_json.dumps(datasets)},
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: {str(show_legend).lower()}, position: 'top',
            labels: {{ color: txt, font: {{ size: 20, weight: 500 }}, boxWidth: 22, boxHeight: 14, padding: 18 }} }},
          tooltip: {{
            backgroundColor: 'rgba(0,0,0,0.85)',
            titleFont: {{ size: 20 }}, bodyFont: {{ size: 20 }},
            padding: 14, cornerRadius: 8,
          }},
        }},
        scales: {{
          x: {{
            grid: {{ display: false }},
            ticks: {{ color: txt, font: {{ size: 20 }}, maxRotation: 30 }},
          }},
          y: {{
            beginAtZero: true,
            grid: {{ color: grid }},
            ticks: {{ color: txt, font: {{ size: 20 }} }},
          }},
        }},
      }},
    }});
  }});
</script>"""


def _render_chart_with_bullets(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    bullets_html = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    chart_type, labels, values, series_labels = _chart_data_from_plan(plan)
    chart_id = f"chart-{abs(hash((title, str(values)))) % 100000}"
    chart_js = _chart_js(plan, theme, chart_id, labels, values, chart_type, series_labels)
    return f"""<style>
{css}
/* === CHART WITH BULLETS === */
.slide--chart-with-bullets {{
  width: 1920px; height: 1080px; background: var(--bg-primary, #fff);
  color: var(--text-primary, #0a0a0a); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-columns: 1fr 1fr; gap: 64px;
}}
.chart-title {{ grid-column: 1 / -1; font-family: var(--font-display);
  font-weight: 700; font-size: 64px; margin: 0; }}
.chart-wrap {{ display: flex; align-items: center; justify-content: center; }}
.bullets {{ font-size: 32px; line-height: 1.5; padding-left: 32px; }}
.bullets li {{ margin-bottom: 24px; }}
</style>
<section class="slide slide--chart-with-bullets">
  <h2 class="chart-title">{title}</h2>
  <div class="chart-wrap">
    <canvas id="{chart_id}" style="width: 100%; height: 480px;"></canvas>
  </div>
  <ul class="bullets">{bullets_html}</ul>
</section>
{chart_js}"""


def _render_agenda(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Agenda")
    items = "\n".join(
        f'<li class="agenda__item"><span class="agenda__num">{i+1:02d}</span>'
        f'<span class="agenda__label">{html.escape(item)}</span></li>'
        for i, item in enumerate(plan.bullets or [])
    )
    return f"""<style>
{css}
/* === AGENDA === */
.slide--agenda {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.agenda__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.agenda__list {{ list-style: none; padding: 0; margin: 0; font-size: 40px; }}
.agenda__item {{ display: grid; grid-template-columns: 120px 1fr;
  gap: 48px; margin-bottom: 40px; align-items: baseline; }}
.agenda__num {{ font-family: var(--font-display); font-weight: 800;
  color: var(--accent); font-size: 56px; }}
.agenda__label {{ font-weight: 500; }}
</style>
<section class="slide slide--agenda">
  <h2 class="agenda__title">{title}</h2>
  <ol class="agenda__list">{items}</ol>
</section>"""


def _render_insights_bullets(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Key Insights")
    items = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    return f"""<style>
{css}
/* === INSIGHTS BULLETS === */
.slide--insights-bullets {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.insights__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.insights__list {{ font-size: 36px; line-height: 1.5; }}
.insights__list li {{ margin-bottom: 32px; }}
</style>
<section class="slide slide--insights-bullets">
  <h2 class="insights__title">{title}</h2>
  <ul class="insights__list">{items}</ul>
</section>"""


def _render_recommendations(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Recommendations")
    items = "\n".join(
        f'<div class="rec"><span class="rec__num">{i+1:02d}</span>'
        f'<p class="rec__text">{html.escape(b)}</p></div>'
        for i, b in enumerate(plan.bullets or [])
    )
    return f"""<style>
{css}
/* === RECOMMENDATIONS === */
.slide--recommendations {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.recs__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.rec {{ display: grid; grid-template-columns: 120px 1fr; gap: 48px;
  margin-bottom: 48px; align-items: start; }}
.rec__num {{ font-family: var(--font-display); font-weight: 800;
  font-size: 72px; color: var(--accent); }}
.rec__text {{ font-size: 32px; line-height: 1.4; margin: 0; }}
</style>
<section class="slide slide--recommendations">
  <h2 class="recs__title">{title}</h2>
  {items}
</section>"""


def _render_methodology(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Methodology")
    bullets = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    return f"""<style>
{css}
/* === METHODOLOGY === */
.slide--methodology {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.method__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.method__body {{ font-size: 32px; line-height: 1.5; padding-left: 32px; }}
.method__body li {{ margin-bottom: 24px; }}
</style>
<section class="slide slide--methodology">
  <h2 class="method__title">{title}</h2>
  <ol class="method__body">{bullets}</ol>
</section>"""


def _render_section_divider(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    subtitle_html = f'<p class="divider__sub">{subtitle}</p>' if subtitle else ""
    hero_css = ""
    if settings.DECK_HERO_ART_ENABLED:
        if plan.hero_image:
            hero_css = f"background-image: url('{html.escape(plan.hero_image, quote=True)}');"
        else:
            hero_css = hero_background_css(theme, plan.title or "divider", "divider")
    return f"""<style>
{css}
/* === SECTION DIVIDER === */
.slide--section-divider {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  background-size: cover;
  {hero_css}
  color: var(--text-primary); font-family: var(--font-body);
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 96px; box-sizing: border-box;
  overflow: hidden; text-align: center;
}}
.divider__title {{ font-family: var(--font-display); font-weight: 900;
  font-size: 144px; margin: 0 0 32px 0; line-height: 1; }}
.divider__sub {{ font-size: 36px; opacity: 0.7; margin: 0; }}
</style>
<section class="slide slide--section-divider">
  <h1 class="divider__title">{title}</h1>
  {subtitle_html}
</section>"""


def _render_closing(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Thank you")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    subtitle_html = f'<p class="closing__sub">{subtitle}</p>' if subtitle else ""
    hero_css = ""
    if settings.DECK_HERO_ART_ENABLED:
        if plan.hero_image:
            hero_css = f"background-image: url('{html.escape(plan.hero_image, quote=True)}');"
        else:
            hero_css = hero_background_css(theme, plan.title or "closing", "closing")
    return f"""<style>
{css}
/* === CLOSING === */
.slide--closing {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  background-size: cover;
  {hero_css}
  color: var(--text-primary); font-family: var(--font-body);
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 96px; box-sizing: border-box;
  overflow: hidden; text-align: center;
}}
.closing__title {{ font-family: var(--font-display); font-weight: 900;
  font-size: 160px; margin: 0 0 32px 0; line-height: 1; }}
.closing__sub {{ font-size: 36px; opacity: 0.7; margin: 0; }}
</style>
<section class="slide slide--closing">
  <h1 class="closing__title">{title}</h1>
  {subtitle_html}
</section>"""


def _render_findings_cards(plan: SlidePlan, theme: ThemePreset) -> str:
    """Render findings cards.

    The SlidePlan contract has no ``cards`` field; we derive cards from
    ``plan.bullets`` (each bullet becomes one card).  Bullets can use
    ``—`` or ``:`` to split title from body; otherwise the whole bullet
    is the title and body is empty.
    """
    css = _theme_css(theme)
    title = html.escape(plan.title or "Findings")
    cards_html_parts = []
    for b in (plan.bullets or []):
        # Try to split on em-dash, colon, or hyphen
        for sep in (" — ", " - ", ": "):
            if sep in b:
                ct, cb = b.split(sep, 1)
                break
        else:
            ct, cb = b, ""
        cards_html_parts.append(
            f'<div class="card">'
            f'<h3 class="card__title">{html.escape(ct.strip())}</h3>'
            f'<p class="card__body">{html.escape(cb.strip())}</p>'
            f'</div>'
        )
    cards_html = "\n".join(cards_html_parts)
    return f"""<style>
{css}
/* === FINDINGS CARDS === */
.slide--findings-cards {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.findings__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.findings__grid {{ display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 48px; }}
.card {{ background: rgba(0,0,0,0.04); padding: 48px;
  border-radius: 12px; border-top: 6px solid var(--accent); }}
.card__title {{ font-family: var(--font-display); font-size: 32px;
  font-weight: 700; margin: 0 0 24px 0; }}
.card__body {{ font-size: 24px; line-height: 1.4; margin: 0; }}
</style>
<section class="slide slide--findings-cards">
  <h2 class="findings__title">{title}</h2>
  <div class="findings__grid">{cards_html}</div>
</section>"""


def _render_chart_full(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    chart_type, labels, values, series_labels = _chart_data_from_plan(plan)
    chart_id = f"chart-full-{abs(hash((title, str(values)))) % 100000}"
    chart_js = _chart_js(plan, theme, chart_id, labels, values, chart_type, series_labels)
    return f"""<style>
{css}
/* === CHART FULL === */
.slide--chart-full {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-rows: auto 1fr; gap: 48px;
}}
.chart-full__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0; }}
.chart-full__canvas-wrap {{ position: relative; height: 760px; }}
</style>
<section class="slide slide--chart-full">
  <h2 class="chart-full__title">{title}</h2>
  <div class="chart-full__canvas-wrap">
    <canvas id="{chart_id}"></canvas>
  </div>
</section>
{chart_js}"""


def _render_data_table(plan: SlidePlan, theme: ThemePreset) -> str:
    """Simple data table — top-8 rows, theme-styled.

    Uses ``plan.table_cols`` for column headers and ``plan.table_rows``
    for the body.  Falls back to inferring columns from the first row
    when ``table_cols`` is empty.
    """
    css = _theme_css(theme)
    title = html.escape(plan.title or "Data Table")
    rows = (plan.table_rows or [])[:8]
    if not rows:
        # Never emit a bare <section> — it inherits the black stage body.
        # Render a styled empty-state with the theme CSS (2026-08-29).
        return f"""<style>
{css}
/* === DATA TABLE (empty state) === */
.slide--data-table {{
  width: 1920px; height: 1080px; background: var(--bg-primary, #ffffff);
  color: var(--text-primary, #0a0a0a); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
</style>
<section class="slide slide--data-table">
  <h2 class="dt__title">{title}</h2>
  <p style="font-size:32px;opacity:0.6;">No data rows available.</p>
</section>"""

    headers = plan.table_cols or list(rows[0].keys())
    thead = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    tbody_rows = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(str(r.get(h, '')))}</td>" for h in headers) + "</tr>"
        for r in rows
    )
    return f"""<style>
{css}
/* === DATA TABLE === */
.slide--data-table {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.dt__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.dt__table {{ width: 100%; border-collapse: collapse; font-size: 24px; }}
.dt__table th {{ text-align: left; padding: 16px;
  background: var(--accent); color: var(--text-primary); }}
.dt__table td {{ padding: 16px; border-bottom: 1px solid rgba(0,0,0,0.1); }}
.dt__table tr:nth-child(even) {{ background: rgba(0,0,0,0.03); }}
</style>
<section class="slide slide--data-table">
  <h2 class="dt__title">{title}</h2>
  <table class="dt__table">
    <thead><tr>{thead}</tr></thead>
    <tbody>{tbody_rows}</tbody>
  </table>
</section>"""


# --- new archetypes (2026-08-29): timeline / roadmap / comparison / swot / quote / process_flow ----


def _render_timeline(plan: SlidePlan, theme: ThemePreset) -> str:
    """Horizontal milestone timeline — bullets become dated milestones.

    Each bullet may use ``|`` to split date from label (e.g. ``Q3 2026 | Pilot``);
    otherwise the whole bullet is the label and the date column is empty.
    """
    css = _theme_css(theme)
    title = html.escape(plan.title or "Timeline")
    bullets = (plan.bullets or [])[:6]
    items: list[str] = []
    n = len(bullets)
    for i, b in enumerate(bullets):
        date, _, label = b.partition("|")
        date = date.strip()
        label = label.strip() or date
        pos = "start" if i == 0 else ("end" if i == n - 1 else "mid")
        items.append(
            f'<div class="tl__item tl__{pos}">'
            f'<div class="tl__dot"></div>'
            f'<p class="tl__date">{html.escape(date)}</p>'
            f'<p class="tl__label">{html.escape(label)}</p>'
            f"</div>"
        )
    return f"""<style>
{css}
/* === TIMELINE === */
.slide--timeline {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.tl__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 120px 0; }}
.tl__row {{ display: grid; grid-template-columns: repeat({max(n,1)}, 1fr);
  gap: 24px; position: relative; }}
.tl__row::before {{ content: ""; position: absolute; top: 14px; left: 3%;
  right: 3%; height: 4px; background: var(--accent); opacity: 0.5;
  border-radius: 2px; }}
.tl__item {{ position: relative; padding-top: 48px; text-align: center; }}
.tl__dot {{ position: absolute; top: 4px; left: 50%; transform: translateX(-50%);
  width: 24px; height: 24px; border-radius: 50%;
  background: var(--bg-primary); border: 6px solid var(--accent);
  box-shadow: 0 0 0 4px var(--bg-primary); }}
.tl__date {{ font-family: var(--font-display); font-weight: 700;
  font-size: 30px; color: var(--accent); margin: 0 0 12px 0; }}
.tl__label {{ font-size: 26px; line-height: 1.35; margin: 0; opacity: 0.92; }}
</style>
<section class="slide slide--timeline">
  <h2 class="tl__title">{title}</h2>
  <div class="tl__row">{''.join(items)}</div>
</section>"""


def _render_roadmap(plan: SlidePlan, theme: ThemePreset) -> str:
    """Three-phase roadmap — Now / Next / Later columns from bullets.

    Bullets use ``phase|item`` (e.g. ``Now|Launch pilot``); phases collapse
    into three buckets in order. Phase names default to Now / Next / Later.
    """
    css = _theme_css(theme)
    title = html.escape(plan.title or "Roadmap")
    bullets = (plan.bullets or [])[:9]
    buckets: dict[str, list[str]] = {"Now": [], "Next": [], "Later": []}
    order = ["Now", "Next", "Later"]
    for b in bullets:
        phase, _, item = b.partition("|")
        phase = phase.strip()
        item = (item.strip() or phase)
        key = phase if phase in buckets else "Later"
        buckets[key].append(item)
    cols = ""
    for i, phase in enumerate(order):
        items_html = "".join(
            f'<li class="rm__item">{html.escape(it)}</li>' for it in buckets[phase]
        ) or '<li class="rm__item rm__empty">—</li>'
        cols += (
            f'<div class="rm__col rm__col-{i+1}">'
            f'<p class="rm__phase">{phase}</p>'
            f'<ul class="rm__list">{items_html}</ul>'
            f"</div>"
        )
    return f"""<style>
{css}
/* === ROADMAP === */
.slide--roadmap {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.rm__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 80px 0; }}
.rm__row {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 40px; }}
.rm__col {{ border-radius: 24px; padding: 40px; background: var(--bg-secondary, rgba(255,255,255,0.04));
  border: 1px solid rgba(255,255,255,0.08); }}
.rm__col-1 {{ border-top: 8px solid var(--accent); }}
.rm__col-2 {{ border-top: 8px solid var(--primary); }}
.rm__col-3 {{ border-top: 8px solid var(--muted); }}
.rm__phase {{ font-family: var(--font-display); font-weight: 800;
  font-size: 40px; margin: 0 0 32px 0; }}
.rm__list {{ list-style: none; padding: 0; margin: 0; }}
.rm__item {{ font-size: 26px; line-height: 1.4; padding: 14px 0;
  border-bottom: 1px solid rgba(255,255,255,0.07); }}
.rm__item:last-child {{ border-bottom: none; }}
.rm__empty {{ opacity: 0.4; }}
</style>
<section class="slide slide--roadmap">
  <h2 class="rm__title">{title}</h2>
  <div class="rm__row">{cols}</div>
</section>"""


def _render_comparison(plan: SlidePlan, theme: ThemePreset) -> str:
    """Two-column comparison — bullets as ``A vs B`` rows.

    Each bullet may use ``||`` to split left/right cells; otherwise `` vs ``
    splits the row. Left column = A (accent), right column = B (muted).
    """
    css = _theme_css(theme)
    title = html.escape(plan.title or "Comparison")
    left_head = html.escape(plan.subtitle or "Option A")
    right_head = html.escape(plan.notes or "Option B") if plan.notes else "Option B"
    rows: list[str] = []
    for b in (plan.bullets or [])[:8]:
        if "||" in b:
            a, _, bb = b.partition("||")
        elif " vs " in b:
            a, _, bb = b.partition(" vs ")
        else:
            a, bb = b, "—"
        rows.append(
            f'<div class="cmp__row">'
            f'<div class="cmp__cell cmp__cell-a">{html.escape(a.strip())}</div>'
            f'<div class="cmp__cell cmp__cell-b">{html.escape(bb.strip())}</div>'
            f"</div>"
        )
    return f"""<style>
{css}
/* === COMPARISON === */
.slide--comparison {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.cmp__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.cmp__head {{ display: grid; grid-template-columns: 1fr 1fr; gap: 40px;
  margin-bottom: 24px; }}
.cmp__head-a {{ font-family: var(--font-display); font-weight: 800;
  font-size: 36px; color: var(--accent); }}
.cmp__head-b {{ font-family: var(--font-display); font-weight: 800;
  font-size: 36px; color: var(--muted); }}
.cmp__row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 40px;
  padding: 20px 0; border-bottom: 1px solid rgba(255,255,255,0.07); }}
.cmp__cell {{ font-size: 28px; line-height: 1.35; }}
.cmp__cell-a {{ color: var(--text-primary); }}
.cmp__cell-b {{ color: var(--muted); }}
</style>
<section class="slide slide--comparison">
  <h2 class="cmp__title">{title}</h2>
  <div class="cmp__head"><div class="cmp__head-a">{left_head}</div><div class="cmp__head-b">{right_head}</div></div>
  {''.join(rows)}
</section>"""


def _render_swot(plan: SlidePlan, theme: ThemePreset) -> str:
    """2x2 SWOT grid. Bullets use ``S|text`` / ``W|text`` / ``O|text`` / ``T|text``
    prefixes; otherwise the first four bullets become S/W/O/T in order.
    """
    css = _theme_css(theme)
    title = html.escape(plan.title or "SWOT")
    quad: dict[str, list[str]] = {"S": [], "W": [], "O": [], "T": []}
    for b in (plan.bullets or [])[:16]:
        key = b[:1].upper()
        if key in quad:
            quad[key].append(b[1:].strip().lstrip("|").strip() or b)
        else:
            quad["S"].append(b)
    cells = ""
    for key, label, color in (
        ("S", "Strengths", "var(--delta_up)"),
        ("W", "Weaknesses", "var(--delta_down)"),
        ("O", "Opportunities", "var(--accent)"),
        ("T", "Threats", "var(--warn_accent, #F59E0B)"),
    ):
        items = "".join(f"<li>{html.escape(it)}</li>" for it in quad[key][:4]) or "<li>—</li>"
        cells += (
            f'<div class="swot__cell">'
            f'<p class="swot__label" style="color:{color}">{label}</p>'
            f'<ul class="swot__list">{items}</ul>'
            f"</div>"
        )
    return f"""<style>
{css}
/* === SWOT === */
.slide--swot {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.swot__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.swot__grid {{ display: grid; grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr; gap: 32px; height: 740px; }}
.swot__cell {{ border-radius: 24px; padding: 36px;
  background: var(--bg-secondary, rgba(255,255,255,0.04));
  border: 1px solid rgba(255,255,255,0.08); }}
.swot__label {{ font-family: var(--font-display); font-weight: 800;
  font-size: 34px; margin: 0 0 20px 0; }}
.swot__list {{ list-style: none; padding: 0; margin: 0; }}
.swot__list li {{ font-size: 24px; line-height: 1.35; padding: 10px 0;
  border-bottom: 1px solid rgba(255,255,255,0.06); }}
.swot__list li:last-child {{ border-bottom: none; }}
</style>
<section class="slide slide--swot">
  <h2 class="swot__title">{title}</h2>
  <div class="swot__grid">{cells}</div>
</section>"""


def _render_quote(plan: SlidePlan, theme: ThemePreset) -> str:
    """Big pull quote — title = quote, subtitle = attribution."""
    css = _theme_css(theme)
    quote = html.escape(plan.title or "")
    attribution = html.escape(plan.subtitle or "") if plan.subtitle else ""
    attr_html = f'<p class="quote__attr">— {attribution}</p>' if attribution else ""
    return f"""<style>
{css}
/* === QUOTE === */
.slide--quote {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 120px; box-sizing: border-box;
  overflow: hidden; text-align: center;
}}
.quote__mark {{ font-family: var(--font-display); font-weight: 900;
  font-size: 220px; line-height: 0.5; color: var(--accent); opacity: 0.5;
  margin-bottom: 48px; }}
.quote__text {{ font-family: var(--font-display); font-weight: 600;
  font-size: 72px; line-height: 1.2; margin: 0 0 48px 0; max-width: 1600px; }}
.quote__attr {{ font-size: 36px; opacity: 0.75; margin: 0; }}
</style>
<section class="slide slide--quote">
  <p class="quote__mark">“</p>
  <p class="quote__text">{quote}</p>
  {attr_html}
</section>"""


def _render_process_flow(plan: SlidePlan, theme: ThemePreset) -> str:
    """Numbered chevron process flow — bullets become steps 1..N."""
    css = _theme_css(theme)
    title = html.escape(plan.title or "Process")
    steps: list[str] = []
    for i, b in enumerate((plan.bullets or [])[:6], start=1):
        steps.append(
            f'<div class="pf__step">'
            f'<p class="pf__num">{i:02d}</p>'
            f'<p class="pf__text">{html.escape(b)}</p>'
            f"</div>"
        )
    return f"""<style>
{css}
/* === PROCESS FLOW === */
.slide--process_flow {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.pf__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 100px 0; }}
.pf__row {{ display: grid; grid-template-columns: repeat({max(len((plan.bullets or [])[:6]),1)}, 1fr);
  gap: 32px; }}
.pf__step {{ position: relative; padding-top: 20px; }}
.pf__num {{ font-family: var(--font-display); font-weight: 900;
  font-size: 96px; color: var(--accent); opacity: 0.35; margin: 0 0 8px 0; }}
.pf__text {{ font-size: 26px; line-height: 1.4; margin: 0; }}
.pf__step:not(:last-child)::after {{ content: "›"; position: absolute;
  top: 40px; right: -28px; font-size: 56px; color: var(--accent);
  opacity: 0.5; }}
</style>
<section class="slide slide--process_flow">
  <h2 class="pf__title">{title}</h2>
  <div class="pf__row">{''.join(steps)}</div>
</section>"""


# Per-layout dispatch.
_LAYOUT_RENDERERS: Dict[str, Any] = {
    "cover": _render_cover,
    "kpi_grid": _render_kpi_grid,
    "chart_with_bullets": _render_chart_with_bullets,
    "agenda": _render_agenda,
    "insights_bullets": _render_insights_bullets,
    "recommendations": _render_recommendations,
    "methodology": _render_methodology,
    "section_divider": _render_section_divider,
    "closing": _render_closing,
    "findings_cards": _render_findings_cards,
    "chart_full": _render_chart_full,
    "data_table": _render_data_table,
    # New archetypes (2026-08-29)
    "timeline": _render_timeline,
    "roadmap": _render_roadmap,
    "comparison": _render_comparison,
    "swot": _render_swot,
    "quote": _render_quote,
    "process_flow": _render_process_flow,
}


def render_slide(layout: str, plan: SlidePlan, theme: ThemePreset) -> str:
    """Render one slide as an HTML ``<section>`` string.

    Raises ``NotImplementedError`` for layouts that haven't been built yet.
    """
    renderer = _LAYOUT_RENDERERS.get(layout)
    if renderer is None:
        raise NotImplementedError(
            f"Layout {layout!r} not implemented yet. "
            f"Available: {sorted(_LAYOUT_RENDERERS)}"
        )
    return renderer(plan, theme)


# Minimal inline copy of frontend-slides' viewport-base.css (the bare
# essentials: fixed 16:9 stage, no scrollbars, page breaks for print).
_STAGE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
@page { size: 1920px 1080px; margin: 0; }
html, body {
  width: 1920px;
  font-family: 'Work Sans', sans-serif;
  background: #000;
  margin: 0;
  padding: 0;
}
.slide {
  width: 1920px;
  height: 1080px;
  position: relative;
  overflow: hidden;
  page-break-after: always;
  break-after: page;
  display: block;
  page-break-inside: avoid;
  break-inside: avoid;
}
.slide:last-child { page-break-after: auto; break-after: auto; }
"""


def build_stage(
    slide_htmls: List[str],
    source_label: str = "",
    deck_title: str = "",
    show_furniture: bool = True,
) -> str:
    """Wrap a list of per-slide HTML strings into a printable index.html.

    The output is a full HTML document LibreOffice can convert to PDF
    (one page per slide) and python-pptx can then turn into a 16:9 deck
    of image-fill slides.

    ``source_label`` (when non-empty) is baked into every slide as a
    ``.source-footer`` element so the rendered PNG carries the citation
    visually — the image-fill PPTX has no text frames to audit, so the
    footer must be part of the image.

    ``deck_title`` (when non-empty) enables professional slide furniture:
    a page number ``NN / TOTAL`` bottom-right and a deck-title footer
    bottom-left on every slide except cover / section dividers / closing
    (those hero pages carry their own full-bleed composition). This is
    the single biggest "looks professionally made" signal — Kimi/Claude
    decks never show a bare content page without page furniture.
    """
    footer_css = """
.source-footer {
  position: absolute;
  left: 64px;
  bottom: 40px;
  font-family: 'Work Sans', sans-serif;
  font-size: 22px;
  color: var(--text-primary, #0a0a0a);
  opacity: 0.55;
  letter-spacing: 0.02em;
}
"""
    furniture_css = """
.deck-furniture {
  position: absolute;
  left: 64px; right: 64px; bottom: 32px;
  display: flex; align-items: center; justify-content: space-between;
  font-family: 'Work Sans', sans-serif;
  font-size: 22px;
  color: var(--text-primary, #0a0a0a);
  opacity: 0.45;
  letter-spacing: 0.03em;
  pointer-events: none;
  z-index: 5;
}
.deck-furniture .deck-furniture__title {
  max-width: 70%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.deck-furniture .deck-furniture__pageno {
  font-variant-numeric: tabular-nums;
}
/* hero pages keep their own centered composition — no furniture there */
.slide--cover .deck-furniture,
.slide--section-divider .deck-furniture,
.slide--closing .deck-furniture {
  display: none;
}
"""
    body_parts: List[str] = []
    total = len(slide_htmls)
    for i, slide_html in enumerate(slide_htmls, start=1):
        chunk = slide_html
        if deck_title and show_furniture:
            furniture = (
                "<div class='deck-furniture'>"
                f"<span class='deck-furniture__title'>{deck_title}</span>"
                f"<span class='deck-furniture__pageno'>{i:02d} / {total:02d}</span>"
                "</div>"
            )
            chunk = _re.sub(r"</section>", furniture + "</section>", chunk, count=1)
        body_parts.append(chunk)
    body = "\n".join(body_parts)
    if source_label:
        footer = (
            "<div class='source-footer'>Source: "
            + source_label.replace("<", "&lt;").replace(">", "&gt;")
            + "</div>"
        )
        # inject before the closing </section> of every slide
        body = _re.sub(
            r"</section>",
            footer + "</section>",
            body,
        )
        _STAGE_CSS_FULL = _STAGE_CSS + footer_css
        if deck_title and show_furniture:
            _STAGE_CSS_FULL += furniture_css
    else:
        _STAGE_CSS_FULL = _STAGE_CSS
        if deck_title and show_furniture:
            _STAGE_CSS_FULL += furniture_css
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Generated Deck</title>
<style>{_STAGE_CSS_FULL}</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Space+Grotesk:wght@400;500&family=Manrope:wght@400;500;800&family=Syne:wght@700;800&family=Fraunces:wght@400;700&family=Work+Sans:wght@400;500&family=Caveat&family=DM+Serif+Display&family=DM+Sans&family=Plus+Jakarta+Sans&family=Orbitron&family=Rajdhani&family=JetBrains+Mono&family=Crimson+Pro&family=Source+Sans+3:wght@400;500&display=swap" rel="stylesheet">
</head>
<body>
{body}
</body>
</html>"""


__all__ = ["render_slide", "build_stage"]
