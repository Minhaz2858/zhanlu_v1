"""Seed recipes — five generic report templates.

All recipes use universal metric references (resolved at runtime via
MetricDefinition). NO domain-specific SQL, table names, or examples.
Recipes are data, not code — the runner executes them deterministically.
"""

SEED_RECIPES: list[dict] = [
    {
        "name": "sales",
        "description": "Sales summary report with revenue, order count, and top customers.",
        "required_metrics": ["total_revenue", "order_count", "avg_order_value"],
        "optional_dimensions": ["region", "product_category", "time_period"],
        "sections": [
            {"title": "Revenue Summary", "source_key": "total_revenue"},
            {"title": "Order Count", "source_key": "order_count"},
            {"title": "Average Order Value", "source_key": "avg_order_value"},
        ],
        "validation_rules": [
            {"rule": "non_empty", "source_key": "total_revenue"},
        ],
        "output_format": "markdown",
        "is_enabled": True,
    },
    {
        "name": "weekly",
        "description": "Weekly summary with key metrics and week-over-week comparison.",
        "required_metrics": ["weekly_revenue", "weekly_order_count", "weekly_growth_rate"],
        "optional_dimensions": ["week", "region"],
        "sections": [
            {"title": "Weekly Revenue", "source_key": "weekly_revenue"},
            {"title": "Weekly Orders", "source_key": "weekly_order_count"},
            {"title": "Growth Rate", "source_key": "weekly_growth_rate"},
        ],
        "validation_rules": [
            {"rule": "non_empty", "source_key": "weekly_revenue"},
        ],
        "output_format": "markdown",
        "is_enabled": True,
    },
    {
        "name": "monthly",
        "description": "Monthly summary with metrics, trends, and month-over-month comparison.",
        "required_metrics": ["monthly_revenue", "monthly_order_count", "monthly_avg_value"],
        "optional_dimensions": ["month", "region", "product_category"],
        "sections": [
            {"title": "Monthly Revenue", "source_key": "monthly_revenue"},
            {"title": "Monthly Orders", "source_key": "monthly_order_count"},
            {"title": "Average Value", "source_key": "monthly_avg_value"},
        ],
        "validation_rules": [
            {"rule": "non_empty", "source_key": "monthly_revenue"},
        ],
        "output_format": "markdown",
        "is_enabled": True,
    },
    {
        "name": "inventory",
        "description": "Current inventory levels and turnover summary.",
        "required_metrics": ["current_stock", "inventory_turnover", "low_stock_items"],
        "optional_dimensions": ["warehouse", "product_category"],
        "sections": [
            {"title": "Current Stock", "source_key": "current_stock"},
            {"title": "Inventory Turnover", "source_key": "inventory_turnover"},
            {"title": "Low Stock Items", "source_key": "low_stock_items"},
        ],
        "validation_rules": [
            {"rule": "non_empty", "source_key": "current_stock"},
        ],
        "output_format": "markdown",
        "is_enabled": True,
    },
    {
        "name": "customer",
        "description": "Customer activity summary with top customers and engagement metrics.",
        "required_metrics": ["customer_count", "active_customers", "top_customers_by_revenue"],
        "optional_dimensions": ["region", "segment"],
        "sections": [
            {"title": "Total Customers", "source_key": "customer_count"},
            {"title": "Active Customers", "source_key": "active_customers"},
            {"title": "Top Customers", "source_key": "top_customers_by_revenue"},
        ],
        "validation_rules": [
            {"rule": "non_empty", "source_key": "customer_count"},
        ],
        "output_format": "markdown",
        "is_enabled": True,
    },
]


def seed_recipes(db) -> int:
    """Insert seed recipes into the DB (idempotent on name). Returns count inserted."""
    from app.models.report_recipe import ReportRecipe

    count = 0
    for spec in SEED_RECIPES:
        existing = (
            db.query(ReportRecipe)
            .filter(ReportRecipe.name == spec["name"])
            .first()
        )
        if existing:
            continue
        recipe = ReportRecipe(
            name=spec["name"],
            description=spec.get("description"),
            required_metrics=spec.get("required_metrics"),
            optional_dimensions=spec.get("optional_dimensions"),
            sections=spec.get("sections"),
            validation_rules=spec.get("validation_rules"),
            output_format=spec.get("output_format", "markdown"),
            is_enabled=spec.get("is_enabled", True),
        )
        db.add(recipe)
        count += 1
    db.commit()
    return count
