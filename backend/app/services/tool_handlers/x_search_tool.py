"""x_search tool — search X (Twitter) via the v2 API.

Hermes port — uses tweepy to authenticate and search recent posts. Light
wrapper that returns {text, author, created_at, metrics} per result.

Env vars: TWITTER_BEARER_TOKEN. (X API v2 supports app-only auth with a
bearer token — no need for full OAuth.)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

X_SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"


async def _x_search(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["TWITTER_BEARER_TOKEN"])
    if missing:
        return missing_config_response("x_search", missing_env=missing)

    query = (args.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "query is required"}
    limit = min(int(args.get("limit", 10)), 100)

    headers = {
        "Authorization": f"Bearer {os.environ['TWITTER_BEARER_TOKEN']}",
        "Content-Type": "application/json",
    }
    params = {
        "query": query,
        "max_results": str(limit),
        "tweet.fields": "created_at,public_metrics,author_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(X_SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"success": False, "error": f"X search failed: {exc}"}
    tweets = data.get("data") or []
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    return {
        "success": True,
        "query": query,
        "count": len(tweets),
        "results": [
            {
                "id": t.get("id"),
                "text": t.get("text"),
                "created_at": t.get("created_at"),
                "author": users.get(t.get("author_id"), {}).get("username"),
                "metrics": t.get("public_metrics", {}),
            }
            for t in tweets
        ],
    }


X_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "x_search",
        "description": (
            "Search recent X (Twitter) posts. Requires TWITTER_BEARER_TOKEN "
            "(X API v2 app-only auth). Returns {text, author, created_at, "
            "metrics} per result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "X search query (supports X operators like 'from:user' or '#hashtag')."},
                "limit": {"type": "integer", "description": "Max results (default 10, max 100).", "default": 10},
            },
            "required": ["query"],
        },
    },
}

registry.register(
    name="x_search",
    schema=X_SEARCH_SCHEMA,
    handler=_x_search,
    category="web",
    toolset="web",
    description="Search recent X (Twitter) posts.",
    emoji="𝕏",
    requires_env=["TWITTER_BEARER_TOKEN"],
    max_result_size_chars=20_000,
)
