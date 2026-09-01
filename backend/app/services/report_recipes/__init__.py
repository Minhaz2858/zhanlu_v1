"""Report Recipes package — first-class reusable report templates."""

from app.services.report_recipes.runner import run_recipe, list_recipes, get_recipe

__all__ = ["run_recipe", "list_recipes", "get_recipe"]
