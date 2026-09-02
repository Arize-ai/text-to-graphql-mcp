#!/usr/bin/env python3
"""
FastMCP Server for Text-to-GraphQL MCP Server.
Exposes GraphQL agent functionality as MCP tools for LLM clients using FastMCP.
"""

import os
import sys

# CRITICAL: Disable Rich colors BEFORE any other imports
# This must be the first thing we do to prevent FastMCP from initializing Rich with colors
def is_mcp_mode():
    """Detect if we're running as an MCP server."""
    return (
        not sys.stdin.isatty() or 
        os.environ.get('MCP_MODE', '').lower() == 'true' or
        os.environ.get('RUNNING_AS_MCP_SERVER', '').lower() == 'true'
    )

# Apply patches immediately if in MCP mode
if is_mcp_mode():
    # Set environment variables to disable colors globally
    os.environ['NO_COLOR'] = '1'
    os.environ['FORCE_COLOR'] = '0'
    os.environ['TERM'] = 'dumb'
    os.environ['COLORTERM'] = ''
    os.environ['ANSI_COLORS_DISABLED'] = '1'
    
    # Patch Rich at the module level before it's imported
    import sys
    from unittest.mock import patch
    
    # Create a mock for Rich Console that never uses colors
    class MockRichConsole:
        def __init__(self, *args, **kwargs):
            self.is_terminal = False
            self._color_system = None
            self.file = sys.stderr  # Add file attribute for Rich logging
            self.legacy_windows = False
            self.width = 80
            self.height = 24
            self.options = None
            
        def _detect_color_system(self):
            return None
            
        def print(self, *args, **kwargs):
            # Redirect to stderr with no colors
            print(*args, file=sys.stderr, **{k: v for k, v in kwargs.items() if k != 'style'})
            
        def log(self, *args, **kwargs):
            # Redirect to stderr with no colors
            print(*args, file=sys.stderr, **{k: v for k, v in kwargs.items() if k != 'style'})
    
    # Monkey patch Rich imports
    # Handle __builtins__ being either a module or dict
    if isinstance(__builtins__, dict):
        original_import = __builtins__['__import__']
    else:
        original_import = __builtins__.__import__
    
    def patched_import(name, *args, **kwargs):
        module = original_import(name, *args, **kwargs)
        
        # Patch Rich console when it's imported
        if name == 'rich.console' or name.startswith('rich.console'):
            if hasattr(module, 'Console'):
                # Replace Console class with our mock
                original_console = module.Console
                module.Console = MockRichConsole
                
                # Patch existing instances
                if hasattr(module, 'console'):
                    module.console = MockRichConsole()
                if hasattr(module, '_console'):
                    module._console = MockRichConsole()
                    
        elif name == 'rich' and hasattr(module, 'console'):
            # Patch the rich.console module
            module.console.Console = MockRichConsole
            if hasattr(module.console, 'console'):
                module.console.console = MockRichConsole()
        
        return module
    
    # Set the patched import function
    if isinstance(__builtins__, dict):
        __builtins__['__import__'] = patched_import
    else:
        __builtins__.__import__ = patched_import
    
    # Additional print redirection
    original_print = print
    def patched_print(*args, **kwargs):
        if 'file' not in kwargs:
            kwargs['file'] = sys.stderr
        # Strip any color-related kwargs
        kwargs = {k: v for k, v in kwargs.items() if k not in ['style', 'color']}
        return original_print(*args, **kwargs)
    
    # Monkey patch print globally
    import builtins
    builtins.print = patched_print

# Now safe to import other modules
import asyncio
import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastmcp import FastMCP

try:
    from .agent import GraphQLAgent
    from .config import settings
    from .logger import logger, scrub_secrets
    from .error_handlers import validated
    from .schema_cache import schema_cache
except ImportError:
    # Handle imports when running as main module
    from agent import GraphQLAgent
    from config import settings
    from logger import logger, scrub_secrets
    from error_handlers import validated
    from schema_cache import schema_cache


# Initialize FastMCP server
mcp = FastMCP("Text-to-GraphQL MCP Server")

# Initialize the agent and storage
agent = GraphQLAgent()
query_history: Dict[str, Dict[str, Any]] = {}
session_id_map: Dict[str, str] = {}


@mcp.tool()
@validated({
    "query": {"type": "string", "required": True, "min_length": 1, "max_length": 10000},
    "history_id": {"type": "history_id", "required": False}
})
async def generate_graphql_query(
    query: str,
    history_id: Optional[str] = None
) -> str:
    """Generate a GraphQL query from natural language description
    
    Args:
        query: Natural language description of the desired GraphQL query
        history_id: Optional history ID to associate with this query
    
    Returns:
        JSON string containing the generated query, validation result, and history ID
    """
    logger.info(f"MCP: Generating query for: {query}")
    
    # Handle history ID. A counter over query_history is read before the await
    # below and written after it, so concurrent calls would collide and silently
    # overwrite each other's history entry.
    if not history_id:
        history_id = str(uuid4())
    
    if history_id not in session_id_map:
        session_id_map[history_id] = str(uuid4())
    
    result = await agent.generate_query(query, session_id_map[history_id])
    
    # Store in history
    query_history[history_id] = {
        "id": history_id,
        "natural_language_query": query,
        "graphql_query": result.get("graphql_query"),
        "validation_result": result.get("validation_result")
    }
    
    response = {
        "history_id": history_id,
        "natural_language_query": query,
        "graphql_query": result.get("graphql_query"),
        "validation_result": result.get("validation_result")
    }
    if result.get("error"):
        response["error"] = result["error"]
    
    return json.dumps(response, indent=2)


@mcp.tool()
@validated({
    "graphql_query": {"type": "graphql_query", "required": True},
    "natural_language_query": {"type": "string", "required": False, "max_length": 10000},
    "history_id": {"type": "history_id", "required": False}
})
async def validate_graphql_query(
    graphql_query: str,
    natural_language_query: Optional[str] = None,
    history_id: Optional[str] = None
) -> str:
    """Validate and update a GraphQL query
    
    Args:
        graphql_query: The GraphQL query to validate
        natural_language_query: The original natural language query for context
        history_id: Optional history ID to update
    
    Returns:
        JSON string containing the validation result and history ID
    """
    logger.info(f"MCP: Validating query: {graphql_query[:50]}...")
    
    if history_id and history_id not in session_id_map:
        session_id_map[history_id] = str(uuid4())
    session_id = session_id_map.get(history_id or "", "") or str(uuid4())

    result = await agent.validate_query(
        graphql_query, natural_language_query or "", session_id
    )
    
    # Update history
    if history_id and history_id in query_history:
        query_history[history_id]["graphql_query"] = graphql_query
        query_history[history_id]["validation_result"] = result
    else:
        # Create new history entry
        history_id = history_id or str(uuid4())
        query_history[history_id] = {
            "id": history_id,
            "natural_language_query": natural_language_query or "",
            "graphql_query": graphql_query,
            "validation_result": result
        }
    
    response = {
        "history_id": history_id,
        "validation_result": result
    }
    
    return json.dumps(response, indent=2)


@mcp.tool()
@validated({
    "graphql_query": {"type": "graphql_query", "required": True},
    "natural_language_query": {"type": "string", "required": False, "max_length": 10000},
    "history_id": {"type": "history_id", "required": False},
    "variables": {"type": "dict", "required": False}
})
async def execute_graphql_query(
    graphql_query: str,
    natural_language_query: Optional[str] = None,
    history_id: Optional[str] = None,
    variables: Optional[Dict[str, Any]] = None
) -> str:
    """Execute a GraphQL query and optionally visualize the results
    
    Args:
        graphql_query: The GraphQL query to execute
        natural_language_query: The original natural language query for context
        history_id: Optional history ID to update
        variables: Optional variables for the GraphQL query
    
    Returns:
        JSON string containing execution results, visualization, and history ID
    """
    logger.info(f"MCP: Executing query: {graphql_query[:50]}...")
    
    # Execute the query
    execution_result = await agent.execute_query(graphql_query, variables=variables or {})
    
    # Format execution result
    formatted_execution_result = {
        "status": "SUCCESS" if execution_result.get("success") == True else "ERROR",
        "response_data": execution_result.get("data", {}),
        "error": execution_result.get("message", "") if execution_result.get("success") == False else None,
        "execution_time": execution_result.get("execution_time", 0)
    }
    
    # Generate visualization if execution was successful
    visualization_result = None
    if execution_result.get("success") == True:
        visualization_result = await agent.visualize_data(
            execution_result.get("data", {}),
            natural_language_query or ""
        )
    
    # Update history
    if history_id and history_id in query_history:
        query_history[history_id]["execution_result"] = formatted_execution_result
        query_history[history_id]["visualization_result"] = visualization_result
    else:
        # Create new history entry
        history_id = history_id or str(uuid4())
        query_history[history_id] = {
            "id": history_id,
            "natural_language_query": natural_language_query or "",
            "graphql_query": graphql_query,
            "execution_result": formatted_execution_result,
            "visualization_result": visualization_result
        }
    
    response = {
        "history_id": history_id,
        "execution_result": formatted_execution_result,
        "visualization_result": visualization_result
    }
    
    return json.dumps(response, indent=2)


@mcp.tool()
@validated()
async def get_query_history() -> str:
    """Retrieve the history of all queries
    
    Returns:
        JSON string containing all query history
    """
    history = list(query_history.values())
    response = {"history": history}
    
    return json.dumps(response, indent=2)


@mcp.tool()
@validated()
async def get_query_examples() -> str:
    """Get example queries to help users understand what they can ask for
    
    Returns:
        JSON string containing example queries
    """
    # Provide default examples
    examples = [
        {"query": "Get all users with their names and emails"},
        {"query": "Find posts by author ID 123"},
        {"query": "List all products with prices above $50"},
        {"query": "Get user profile including posts and comments"},
        {"query": "Search for posts containing 'GraphQL'"}
    ]
    
    response = {"examples": examples}
    
    return json.dumps(response, indent=2)


@mcp.tool()
@validated()
async def refresh_schema() -> str:
    """Force an immediate re-introspection of the GraphQL schema

    ESCAPE HATCH - do not call this before normal queries. The schema is cached
    and re-introspected automatically once its TTL expires
    (SCHEMA_CACHE_TTL_SECONDS, default 3600), so generate_graphql_query already
    picks up schema changes on its own. Reach for this only when you know the
    schema just changed and cannot wait for the TTL, or when diagnosing a
    suspected stale-schema problem.

    Bypasses the TTL and validates the introspected schema before storing it. If
    the refresh fails, the previously cached schema is left untouched and stays
    in use.

    Returns:
        JSON string reporting the refresh outcome, including whether the schema changed
    """
    logger.info("MCP: Forced schema refresh requested")

    try:
        report = await schema_cache.refresh()
    except Exception as e:
        retained = schema_cache.current()
        response = {
            "refreshed": False,
            "endpoint": schema_cache.endpoint,
            "error": scrub_secrets(e),
            "error_type": type(e).__name__,
            "message": (
                "Schema refresh failed. The previously cached schema was retained "
                "and remains in use."
                if retained is not None
                else "Schema refresh failed and no cached schema is available."
            ),
        }
        if retained is not None:
            response["current_hash"] = retained.schema_hash
            response["cache_age"] = round(retained.cache_age, 3)
        logger.json("ERROR", "Forced schema refresh failed", response)
        return json.dumps(response, indent=2)

    changed = report.get("changed")
    response = {
        "refreshed": True,
        "changed": changed,
        "previous_hash": report.get("previous_hash"),
        "current_hash": report.get("schema_hash"),
        "source": report.get("source"),
        "endpoint": report.get("endpoint"),
        "refreshed_at": report.get("refreshed_at"),
        "message": (
            "Schema refreshed successfully. Changed: "
            + ("yes" if changed else "no")
        ),
    }
    logger.json("INFO", "Forced schema refresh completed", response)

    return json.dumps(response, indent=2)


def main():
    """Main entry point for the MCP server.""" 
    if is_mcp_mode():
        logger.info("=== Text-to-GraphQL MCP Server Starting (MCP Mode - Colors Disabled) ===")
    else:
        logger.info("=== Text-to-GraphQL MCP Server Starting ===")
    
    mcp.run()


if __name__ == "__main__":
    main() 