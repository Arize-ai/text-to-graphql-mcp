"""
Integration tests for the schema lifecycle wiring: the LangGraph nodes that
consume the cache, and the error path back out to the caller.
"""

import importlib

import pytest
from graphql import GraphQLSchema

from src.text_to_graphql_mcp.schema_cache import SOURCE_FRESH_CACHE, SOURCE_LIVE, SchemaCache

from .conftest import TEST_ENDPOINT


async def test_schema_management_node_populates_state(monkeypatch, cache, transport):
    sm = importlib.import_module("src.text_to_graphql_mcp.tools.schema_management")

    monkeypatch.setattr(sm, "schema_cache", cache)
    state = {"intent": {"description": "get all users"}}

    result = await sm.schema_management(state)

    assert isinstance(result["schema"], GraphQLSchema)
    assert result["schema"].query_type.name == "Query"
    assert result.get("tool_error") is None
    assert transport.calls == 1


async def test_schema_management_node_reuses_cache_across_calls(monkeypatch, cache, transport):
    sm = importlib.import_module("src.text_to_graphql_mcp.tools.schema_management")

    monkeypatch.setattr(sm, "schema_cache", cache)

    first = await sm.schema_management({"intent": {"description": "a"}})
    second = await sm.schema_management({"intent": {"description": "b"}})

    # One introspection for two queries, and the same schema object both times.
    assert transport.calls == 1
    assert first["schema"] is second["schema"]


async def test_schema_management_node_records_a_clear_error(monkeypatch, cache_dir, transport):
    sm = importlib.import_module("src.text_to_graphql_mcp.tools.schema_management")

    unconfigured = SchemaCache(cache_dir=cache_dir, transport=transport, endpoint="")
    monkeypatch.setattr(sm, "schema_cache", unconfigured)

    result = await sm.schema_management({"intent": {"description": "get all users"}})

    assert "schema" not in result
    assert "GRAPHQL_ENDPOINT" in result["tool_error"]


async def test_query_validation_loads_schema_from_cache(monkeypatch, cache, transport):
    """validate_graphql_query used to fail on a fresh process because it relied
    on state left behind by a prior generate_graphql_query."""
    qv = importlib.import_module("src.text_to_graphql_mcp.tools.query_validation")

    monkeypatch.setattr(qv, "schema_cache", cache)

    # No schema and no intent in state, exactly as on a fresh process.
    result = await qv.query_validation({"query": "query { hello }"})

    assert result.get("tool_error") is None
    assert result["query_validation_result"]["is_valid"] is True
    assert transport.calls == 1


async def test_query_validation_rejects_an_invalid_query_against_the_cached_schema(
    monkeypatch, cache, transport
):
    qv = importlib.import_module("src.text_to_graphql_mcp.tools.query_validation")

    monkeypatch.setattr(qv, "schema_cache", cache)

    result = await qv.query_validation({"query": "query { nonexistentField }"})

    assert result["query_validation_result"]["is_valid"] is False
    assert result["query_validation_result"]["errors"]


async def test_query_validation_reports_when_no_schema_can_be_loaded(
    monkeypatch, cache_dir, transport
):
    qv = importlib.import_module("src.text_to_graphql_mcp.tools.query_validation")

    transport.fail_message = "connection refused"
    broken = SchemaCache(cache_dir=cache_dir, transport=transport, endpoint=TEST_ENDPOINT)
    monkeypatch.setattr(qv, "schema_cache", broken)

    result = await qv.query_validation({"query": "query { hello }"})

    assert "No GraphQL schema available" in result["tool_error"]
    assert "query_validation_result" not in result


async def test_generate_query_surfaces_a_tool_error(monkeypatch, cache_dir, transport):
    """A schema failure must reach the caller, not just the log."""
    from src.text_to_graphql_mcp import agent as agent_module

    async def failing_graph_run(state):
        return {**state, "tool_error": "Schema management failed: no endpoint"}

    instance = object.__new__(agent_module.GraphQLAgent)
    instance.llm = None
    instance.state = {"messages": []}

    class FakeGraph:
        async def ainvoke(self, state):
            return await failing_graph_run(state)

    instance.graph = FakeGraph()

    response = await agent_module.GraphQLAgent.generate_query(
        instance, "get all users", "session-1"
    )

    assert response["graphql_query"] is None
    assert response["error"] == "Schema management failed: no endpoint"


async def test_second_process_reuses_the_disk_cache(cache_dir, transport):
    """A newly started process must not re-introspect a recent schema."""
    first_process = SchemaCache(
        cache_dir=cache_dir, transport=transport, ttl_seconds=3600, endpoint=TEST_ENDPOINT
    )
    initial = await first_process.get_schema()
    assert initial.source == SOURCE_LIVE
    assert transport.calls == 1

    second_process = SchemaCache(
        cache_dir=cache_dir, transport=transport, ttl_seconds=3600, endpoint=TEST_ENDPOINT
    )
    reloaded = await second_process.get_schema()

    assert reloaded.source == SOURCE_FRESH_CACHE
    assert transport.calls == 1
    assert reloaded.schema_hash == initial.schema_hash
