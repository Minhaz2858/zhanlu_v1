"""Parse sort parameter into SQLAlchemy order_by clauses.

The Base44 SDK sends sort as a string:
- "created_date" -> ORDER BY created_date ASC
- "-created_date" -> ORDER BY created_date DESC
- "updated_date" -> ORDER BY updated_date ASC

Note: The SDK's list() method passes sort directly (e.g. '-updated_date').
The sort param can also be a comma-separated list of fields.
"""

from sqlalchemy import asc, desc


def parse_sort(model, sort: str | None):
    """Parse the sort parameter into a list of SQLAlchemy order_by clauses.

    Args:
        model: SQLAlchemy model class
        sort: Sort string (e.g. "-created_date" or "name,-updated_date")

    Returns:
        List of SQLAlchemy order_by clauses, or empty list if no sort
    """
    if not sort:
        # Default: newest first
        return [desc(model.created_date)]

    clauses = []
    # Support comma-separated sort fields
    for part in sort.split(","):
        part = part.strip()
        if not part:
            continue

        if part.startswith("-"):
            field_name = part[1:]
            col = getattr(model, field_name, None)
            if col is not None:
                clauses.append(desc(col))
        else:
            col = getattr(model, part, None)
            if col is not None:
                clauses.append(asc(col))

    if not clauses:
        clauses = [desc(model.created_date)]

    return clauses
