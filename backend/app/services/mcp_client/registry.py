"""MCP marketplace catalog — curated well-known MCP servers.

These are *templates*: connecting requires the user's own credentials
(OAuth or API key). The catalog powers the MySpace MCP marketplace tab and
``GET /api/mcp/marketplace``.
"""
from __future__ import annotations

from typing import Any

MCP_MARKETPLACE: list[dict[str, Any]] = [
    {
        "id": "github-copilot",
        "name": "GitHub Copilot",
        "description": "GitHub issues, repos, code search and PRs.",
        "transport": "http",
        "base_url": "https://api.githubcopilot.com/mcp/",
        "auth_type": "oauth",
        "category": "Dev",
    },
    {
        "id": "slack",
        "name": "Slack",
        "description": "Read and send messages in Slack workspaces.",
        "transport": "http",
        "base_url": "https://mcp.slack.com/mcp",
        "auth_type": "oauth",
        "category": "Messaging",
    },
    {
        "id": "notion",
        "name": "Notion",
        "description": "Pages, databases, and search in Notion.",
        "transport": "http",
        "base_url": "https://mcp.notion.com/mcp",
        "auth_type": "oauth",
        "category": "Productivity",
    },
    {
        "id": "sentry",
        "name": "Sentry",
        "description": "Issues, events and releases from Sentry.",
        "transport": "http",
        "base_url": "https://mcp.sentry.dev/mcp",
        "auth_type": "oauth",
        "category": "Observability",
    },
    {
        "id": "linear",
        "name": "Linear",
        "description": "Issues, cycles and projects in Linear.",
        "transport": "http",
        "base_url": "https://mcp.linear.app/mcp",
        "auth_type": "oauth",
        "category": "Project Management",
    },
    {
        "id": "figma",
        "name": "Figma",
        "description": "Figma files, frames and design tokens.",
        "transport": "http",
        "base_url": "https://mcp.figma.com/mcp",
        "auth_type": "oauth",
        "category": "Design",
    },
    {
        "id": "fetch",
        "name": "Fetch",
        "description": "Generic HTTP fetch — any REST API (bring your own server).",
        "transport": "http",
        "base_url": "https://example.com/mcp",
        "auth_type": "bearer",
        "category": "Generic",
    },
    {
        "id": "playwright",
        "name": "Playwright (stdio)",
        "description": "Browser automation server (stdio template: npx @playwright/mcp).",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "auth_type": "none",
        "category": "Browser",
    },
]

_ids = {entry["id"] for entry in MCP_MARKETPLACE}


def get_marketplace() -> list[dict[str, Any]]:
    return [dict(entry) for entry in MCP_MARKETPLACE]


def get_marketplace_entry(marketplace_id: str) -> dict[str, Any] | None:
    for entry in MCP_MARKETPLACE:
        if entry["id"] == marketplace_id:
            return dict(entry)
    return None


def is_valid_id(marketplace_id: str) -> bool:
    return marketplace_id in _ids


__all__ = ["MCP_MARKETPLACE", "get_marketplace", "get_marketplace_entry", "is_valid_id"]
