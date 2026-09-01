import logging
logging.basicConfig(level=logging.WARNING)
from app.services.synexia.contracts import DeckPlan, SlidePlan
from app.services.artifacts.skill_deck_profiles import apply_skill_profile
from app.services.artifacts.themes import select_theme

base = DeckPlan(
    title="C5/C9 Market View",
    theme_recommendation="",
    palette_recommendation="",
    deck_type="data_report",
    slides=[
        SlidePlan(layout="cover", title="C5/C9 Market View"),
        SlidePlan(layout="insights_bullets", title="Key Insights", bullets=["A", "B"]),
        SlidePlan(layout="closing", title="Thank You"),
    ],
)

print("=== skill-aware profile application (slides-authored plan, no explicit theme) ===")
for skill in ("kai-slide-creator", "guizang-ppt-skill", "slide-maestro",
              "knowledge-cat-ppt-skill", "agentbuff-presentation", "ppt-design"):
    plan = base.model_copy(deep=True)
    changed = apply_skill_profile(plan, skill)
    preset = select_theme(plan, "make a market view ppt")
    bg = preset.color_tokens.get("bg_primary")
    accent = preset.color_tokens.get("accent")
    print("%-24s changed=%-5s theme=%-20s palette=%-16s deck_type=%-15s bg=%-8s accent=%s" % (
        skill, changed, plan.theme_recommendation, plan.palette_recommendation,
        plan.deck_type, bg, accent))

print()
print("=== explicit planner pick is preserved ===")
plan = base.model_copy(deep=True)
plan.theme_recommendation = "paper_and_ink"
plan.deck_type = "executive_brief"
changed = apply_skill_profile(plan, "kai-slide-creator")
print("changed:", changed, "| theme stays:", plan.theme_recommendation, "| deck_type stays:", plan.deck_type)

print()
print("=== unknown skill is a no-op ===")
plan = base.model_copy(deep=True)
changed = apply_skill_profile(plan, "not-a-real-skill")
print("changed:", changed, "| theme:", repr(plan.theme_recommendation))
