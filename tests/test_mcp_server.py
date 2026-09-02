"""Test cases for the MCP server functionality."""

import json
import pytest
from unittest.mock import AsyncMock, Mock, patch

# Import when running tests - adjust imports as needed for the final package structure
try:
    # Import the MCP tools directly from the correct module
    from src.text_to_graphql_mcp.mcp_server import (
        generate_graphql_query, validate_graphql_query, execute_graphql_query,
        agent, query_history
    )
    from src.text_to_graphql_mcp.agent import GraphQLAgent
except ImportError:
    # Fallback for when tests are run from package install
    from text_to_graphql_mcp.mcp_server import (
        generate_graphql_query, validate_graphql_query, execute_graphql_query,
        agent, query_history
    )
    from text_to_graphql_mcp.agent import GraphQLAgent


class TestMCPServer:
    """Test cases for MCP server tools."""

    @pytest.mark.asyncio
    async def test_generate_graphql_query_success(self):
        """Test successful GraphQL query generation."""
        # Mock the agent's generate_query method
        with patch.object(agent, 'generate_query', new_callable=AsyncMock) as mock_generate:
            mock_generate.return_value = {
                "graphql_query": "query { users { id name } }",
                "validation_result": {"is_valid": True}
            }
            
            # fastmcp's @mcp.tool() returns the undecorated function, so the
            # tool is callable directly (it no longer exposes a .fn attribute).
            result = await generate_graphql_query("Get all users")
            result_data = json.loads(result)
            
            assert "graphql_query" in result_data
            assert "validation_result" in result_data
            assert result_data["natural_language_query"] == "Get all users"
            assert result_data["graphql_query"] == "query { users { id name } }"

    @pytest.mark.asyncio
    async def test_generate_graphql_query_empty_query(self):
        """Test error handling for empty query."""
        # The function should return an error response, not raise an exception
        result = await generate_graphql_query("")
        result_data = json.loads(result)
        
        assert "error" in result_data
        assert result_data["error"] is True
        assert result_data["error_code"] == "VALIDATION_ERROR"
        assert "query" in result_data["message"]  # Error message should mention the query parameter

    @pytest.mark.asyncio
    async def test_validate_graphql_query_success(self):
        """Test successful GraphQL query validation."""
        with patch.object(agent, 'validate_query', new_callable=AsyncMock) as mock_validate:
            mock_validate.return_value = {
                "is_valid": True,
                "errors": []
            }
            
            result = await validate_graphql_query("query { users { id } }")
            result_data = json.loads(result)
            
            assert "validation_result" in result_data
            assert "history_id" in result_data
            assert result_data["validation_result"]["is_valid"] is True

    @pytest.mark.asyncio
    async def test_execute_graphql_query_success(self):
        """Test successful GraphQL query execution."""
        with patch.object(agent, 'execute_query', new_callable=AsyncMock) as mock_execute:
            with patch.object(agent, 'visualize_data', new_callable=AsyncMock) as mock_visualize:
                mock_execute.return_value = {
                    "success": True,
                    "data": {"users": [{"id": "1", "name": "Test User"}]},
                    "execution_time": 0.5
                }
                mock_visualize.return_value = {
                    "visualizations": [],
                    "recommendations": []
                }
                
                result = await execute_graphql_query("query { users { id name } }")
                result_data = json.loads(result)
                
                assert "execution_result" in result_data
                assert "visualization_result" in result_data
                assert result_data["execution_result"]["status"] == "SUCCESS"


class TestGraphQLAgent:
    """Test cases for the GraphQL agent."""

    def test_agent_initialization(self):
        """Test that the agent initializes correctly."""
        agent = GraphQLAgent()
        assert agent is not None
        assert hasattr(agent, 'graph')
        assert hasattr(agent, 'llm')

    @pytest.mark.asyncio
    async def test_generate_query_method_exists(self):
        """Test that the generate_query method exists and is callable."""
        agent = GraphQLAgent()
        assert hasattr(agent, 'generate_query')
        assert callable(getattr(agent, 'generate_query'))

    @pytest.mark.asyncio
    async def test_validate_query_method_exists(self):
        """Test that the validate_query method exists and is callable."""
        agent = GraphQLAgent()
        assert hasattr(agent, 'validate_query')
        assert callable(getattr(agent, 'validate_query'))

    @pytest.mark.asyncio
    async def test_execute_query_method_exists(self):
        """Test that the execute_query method exists and is callable."""
        agent = GraphQLAgent()
        assert hasattr(agent, 'execute_query')
        assert callable(getattr(agent, 'execute_query'))


if __name__ == "__main__":
    pytest.main([__file__]) 