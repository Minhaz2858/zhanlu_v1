"""Parse MongoDB-style query JSON into SQLAlchemy filter conditions.

Supports:
- Simple equality: {"status": "active"} -> model.status == "active"
- $eq: {"status": {"$eq": "active"}}
- $ne: {"status": {"$ne": "draft"}}
- $gt, $gte, $lt, $lte: {"rating": {"$gt": 4.0}}
- $in: {"role": {"$in": ["admin", "user"]}}
- $nin: {"status": {"$nin": ["archived", "deleted"]}}
- Multiple fields: {"status": "active", "project": "global"} -> AND
"""

import json
from typing import Any
from sqlalchemy import and_, or_


def parse_query(model, q: str | None) -> list:
    """Parse the q query parameter into a list of SQLAlchemy filter conditions.

    Args:
        model: SQLAlchemy model class
        q: JSON string query filter (e.g. '{"status":"active"}')

    Returns:
        List of SQLAlchemy filter conditions (combined with AND in the caller)
    """
    if not q:
        return []

    try:
        query_dict = json.loads(q) if isinstance(q, str) else q
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(query_dict, dict) or not query_dict:
        return []

    conditions = []
    for field_name, value in query_dict.items():
        # Skip internal fields that should never be queried
        if field_name in ("is_deleted", "password_hash"):
            continue

        col = getattr(model, field_name, None)
        if col is None:
            continue

        if isinstance(value, dict):
            # MongoDB-style operators
            for op_key, op_value in value.items():
                if op_key == "$eq":
                    conditions.append(col == op_value)
                elif op_key == "$ne":
                    conditions.append(col != op_value)
                elif op_key == "$gt":
                    conditions.append(col > op_value)
                elif op_key == "$gte":
                    conditions.append(col >= op_value)
                elif op_key == "$lt":
                    conditions.append(col < op_value)
                elif op_key == "$lte":
                    conditions.append(col <= op_value)
                elif op_key == "$in":
                    conditions.append(col.in_(op_value))
                elif op_key == "$nin":
                    conditions.append(~col.in_(op_value))
        elif value is None:
            # {"field": null} -> IS NULL
            conditions.append(col.is_(None))
        else:
            # Simple equality
            conditions.append(col == value)

    return conditions
