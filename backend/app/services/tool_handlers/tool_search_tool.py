"""search_tools meta-tool — registered capability (IBM tool-calling P0-2).

Lets the model discover tools that selective injection (tool_search.
select_relevant_tools) filtered out of its schema list. Execution
resolves handlers by name from the ToolRegistry, so a discovered tool is
immediately callable in the next iteration.
"""
from app.services.tool_registry import registry
from app.services.tool_search import handle_search_tools

SEARCH_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tools",
        "description": (
            "Search the full tool catalog by keyword when the tool you need "
            "is not in your current tool list. Returns matching tool names, "
            "descriptions and parameter schemas — call the returned tool "
            "name directly afterwards."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the tool should do.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matches to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
}

registry.register(
    name="search_tools",
    schema=SEARCH_TOOLS_SCHEMA,
    handler=handle_search_tools,
    category="meta",
    enabled_by_default=True,
    description="Search the full tool catalog by keyword.",
)
