"""
Schema management tool for the Text-to-GraphQL MCP Server.

The schema lifecycle (TTL, disk cache, live introspection, forced refresh)
lives in ``text_to_graphql_mcp.schema_cache``; this node just resolves the
current schema into the agent state.
"""
from ..types import AgentState
from ..logger import logger
from ..schema_cache import schema_cache


async def schema_management(state: AgentState) -> AgentState:
    """
    Resolves the GraphQL schema for the current request.

    Serves the cached schema when it is within its TTL and introspects the live
    endpoint only when the cache is stale or missing. On failure it falls back
    to the last known-good schema; if there is none, it records a tool error so
    the workflow stops with a clear message rather than handing downstream tools
    an unusable schema.
    """
    state["current_step"] = "schema_management"
    logger.json("INFO", "Schema management tool called", {
        "tool": "schema_management",
        "intent": state.get("intent"),
    })

    try:
        # Intent is not used to resolve the schema, but downstream tools assume
        # it exists, so keep the original precondition.
        intent = state.get("intent")
        if not intent:
            logger.json("WARNING", "No intent provided for schema management", {
                "tool": "schema_management",
                "error": "missing_intent"
            })
            state["tool_error"] = "No intent provided for schema management"
            return state

        loaded = await schema_cache.get_schema()
        state["schema"] = loaded.schema

        logger.json("INFO", "Schema management completed successfully", {
            "tool": "schema_management",
            **loaded.describe(),
        })
        return state
    except Exception as e:
        error_info = {
            "tool": "schema_management",
            "error_type": type(e).__name__,
            "error_message": str(e)
        }
        logger.json("ERROR", "Error in schema management", error_info)
        state["tool_error"] = f"Schema management failed: {str(e)}"
        return state
