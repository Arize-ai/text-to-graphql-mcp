"""
Query validation tool for the Text-to-GraphQL MCP Server.
"""

from typing import Dict, Any, Optional, List, Union
from ..logger import logger
from ..types import AgentState
from ..schema_cache import schema_cache

import json

from graphql import parse, validate, GraphQLError

def validate_gql_query(schema, query: str) -> tuple[bool, Union[list[str], GraphQLError]]:
    try:
        query_ast = parse(query)
        errors = validate(schema, query_ast)
        if errors:
            return False, errors
        return True, []
    except Exception as e:
        return False, [str(e)]

async def query_validation(state: AgentState) -> AgentState:
    """
    Validates GraphQL queries for correctness and optimization.
    
    Given a constructed GraphQL query and schema information:
    - Checks for syntax errors in the query
    - Verifies that all referenced types and fields exist in the schema
    - Ensures required fields have proper arguments
    - Checks for type consistency in variables and arguments
    - Identifies potential performance issues
    - Suggests optimizations where appropriate
    - Provides human-readable error and suggestion messages
    """
    state["current_step"] = "query_validation"
    
    logger.json("INFO", "Query Validation started", {
        "state": state
    })

    try:
        # Get the query, intent, and schema from the state
        query = state.get("query", "")
        intent = state.get("intent")
        schema = state.get("schema")
        
        if not query:
            logger.json("WARNING", "No query provided for validation", {
                "tool": "query_validation",
                "error": "missing_query"
            })
            state["tool_error"] = "No query provided for validation"
            return state
            
        if schema is None:
            # validate_graphql_query can be called before generate_graphql_query,
            # so resolve the schema from the cache rather than depending on state
            # left behind by an earlier call in the same process.
            try:
                loaded = await schema_cache.get_schema()
                schema = loaded.schema
                state["schema"] = schema
                logger.json("INFO", "Loaded schema from cache for validation", {
                    "tool": "query_validation",
                    **loaded.describe(),
                })
            except Exception as e:
                logger.json("WARNING", "No schema available for validation", {
                    "tool": "query_validation",
                    "error": "missing_schema",
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                })
                state["tool_error"] = f"No GraphQL schema available for query validation: {str(e)}"
                return state
            
        # Log the query being validated
        logger.json("INFO", "Validating GraphQL query", {
            "tool": "query_validation",
            "intent": intent,
            "query_preview": query[:100] + ("..." if len(query) > 100 else ""),
            "schema_available": bool(schema)
        })
        
        # Validate the query
        is_valid, errors = validate_gql_query(schema, query)
        
        # Convert errors to string representation for logging
        error_strings = [str(err) for err in errors] if errors else []

        validation_result = {
            "is_valid": is_valid,
            "errors": error_strings,
        }
        
        # Store the validation result in the state
        state["query_validation_result"] = validation_result
        
        # Log the validation result with appropriate level based on validity
        log_level = "INFO" if is_valid else "WARNING"
        logger.json(log_level, "Query validation result", {
            "tool": "query_validation",
            "is_valid": is_valid,
            "error_count": len(error_strings),
            "errors": error_strings[:3] + (["..."] if len(error_strings) > 3 else [])
        })
        
        return state
    except Exception as e:
        error_info = {
            "tool": "query_validation",
            "error_type": type(e).__name__,
            "error_message": str(e)
        }
        logger.json("ERROR", "Error in query validation", error_info)
        
        state["tool_error"] = f"Query validation failed: {str(e)}"
        return state