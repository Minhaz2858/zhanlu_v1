"""Narrative Generator — LLM prompt templates for intelligence events (Phase 5D).

Port of EDIA 5.1.2 backend/src/intelligence/narrative_generator.py.
Provides structured prompt templates for each event type class.
"""

NARRATIVE_PROMPT_TEMPLATES: dict[str, str] = {
    "geopolitical_supply_disruption": (
        "A geopolitical supply disruption has just occurred.\n\n"
        "Event: {event_headline}\n"
        "Crude impact: {crude_pct:+.1f}%\n"
        "Naphtha impact: {naphtha_pct:+.1f}%\n"
        "Product impacts:\n{product_impact_table}\n\n"
        "Historical precedent: {closest_precedent}\n\n"
        "Write a 4-part forecast narrative:\n\n"
        "PART 1 — WHY THIS MATTERS (3-4 sentences): Trace the exact causal path.\n\n"
        "PART 2 — QUANTIFIED IMPACT (one line per product): State expected CNY impact.\n\n"
        "PART 3 — SCENARIO ANALYSIS: Bull/Base/Bear with probability and conditions.\n\n"
        "PART 4 — IMMEDIATE ACTIONS: Steps in next 24-48 hours."
    ),
    "tariff_policy": (
        "A tariff policy change has been announced.\n\n"
        "Event: {event_headline}\n"
        "Affected: {affected_products}\n"
        "Impact: {product_impact_table}\n"
        "Precedent: {closest_precedent}\n\n"
        "Write covering: demand destruction vs. import substitution, supply chain flow, "
        "time profile, Ecisco-specific customer segment recommendations."
    ),
    "plant_outage": (
        "A significant plant shutdown has been reported.\n\n"
        "Event: {event_headline}\n"
        "Products: {affected_products}\n"
        "Impact: {product_impact_table}\n"
        "Precedent: {closest_precedent}\n\n"
        "Write covering: supply gap calculation, price impact, substitute product demand, "
        "Ecisco's production adjustment recommendations."
    ),
    "crude_oil_shock": (
        "A crude oil price shock has occurred.\n\n"
        "Event: {event_headline}\n"
        "Crude: {crude_pct:+.1f}%\n"
        "Naphtha chain: {naphtha_pct:+.1f}%\n"
        "Product cascade:\n{product_impact_table}\n\n"
        "Write the cascade narrative from crude → naphtha → downstream products."
    ),
    "force_majeure": (
        "Force majeure declaration reported.\n\n"
        "Event: {event_headline}\n"
        "Affected: {affected_products}\n"
        "Impact: {product_impact_table}\n\n"
        "Write covering: force majeure scope, supply impact, alternative sourcing, "
        "Ecisco's contingency plan."
    ),
    "demand_surge": (
        "A demand surge signal detected.\n\n"
        "Event: {event_headline}\n"
        "Products: {affected_products}\n"
        "Impact: {product_impact_table}\n\n"
        "Write covering: demand drivers, supply-demand balance, price trajectory, "
        "Ecisco's inventory preparation recommendations."
    ),
}


def get_narrative_template(event_type: str) -> str:
    """Return the prompt template for a given event type."""
    return NARRATIVE_PROMPT_TEMPLATES.get(
        event_type,
        NARRATIVE_PROMPT_TEMPLATES["crude_oil_shock"],
    )


def format_narrative_prompt(
    event_type: str,
    event_headline: str,
    product_impact_table: str,
    affected_products: str = "",
    crude_pct: float = 0.0,
    naphtha_pct: float = 0.0,
    closest_precedent: str = "None available",
) -> str:
    """Format a narrative prompt with all required fields."""
    template = get_narrative_template(event_type)
    return template.format(
        event_headline=event_headline,
        product_impact_table=product_impact_table,
        affected_products=affected_products,
        crude_pct=crude_pct,
        naphtha_pct=naphtha_pct,
        closest_precedent=closest_precedent,
    )
