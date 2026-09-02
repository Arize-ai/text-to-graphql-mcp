"""
Credential-leakage audit.

Configures an endpoint carrying a password in userinfo, a token in the query
string, and an API key in the auth headers, then asserts none of that material
reaches any output surface: log records, the cache file, the cache filename,
LoadedSchema.describe(), the refresh report, or the refresh_schema tool output
on either the success or the failure path.
"""

import json

import pytest

from src.text_to_graphql_mcp.exceptions import GraphQLSchemaError
from src.text_to_graphql_mcp.logger import redact_url
from src.text_to_graphql_mcp.schema_cache import SchemaCache

from .conftest import SDL_V1

PASSWORD = "p4ssw0rd-in-userinfo"
QUERY_TOKEN = "t0ken-in-query-string"
API_KEY = "k3y-in-auth-header"

SECRETS = (PASSWORD, QUERY_TOKEN, API_KEY)
LEAKY_URL = f"https://svc:{PASSWORD}@api.example.test/v2/graphql?access_token={QUERY_TOKEN}"


@pytest.fixture
def captured_logs():
    """Tee every log record emitted during the test into a list."""
    from src.text_to_graphql_mcp.logger import logger as app_logger

    records = []
    sink_id = app_logger.add(lambda message: records.append(str(message)), level="DEBUG")
    try:
        yield records
    finally:
        app_logger.remove(sink_id)


def assert_no_secrets(haystack, where):
    for secret in SECRETS:
        assert secret not in haystack, f"{secret!r} leaked into {where}"


async def test_no_credential_reaches_any_output_surface(
    cache_dir, transport, monkeypatch, captured_logs
):
    monkeypatch.setattr("src.text_to_graphql_mcp.config.settings.GRAPHQL_API_KEY", API_KEY)
    monkeypatch.setattr("src.text_to_graphql_mcp.config.settings.GRAPHQL_ENDPOINT", LEAKY_URL)

    cache = SchemaCache(
        cache_dir=cache_dir, transport=transport, ttl_seconds=3600, endpoint=LEAKY_URL
    )

    # --- success path
    loaded = await cache.get_schema()
    assert_no_secrets(json.dumps(loaded.describe()), "LoadedSchema.describe()")
    assert_no_secrets(loaded.endpoint, "LoadedSchema.endpoint")
    assert_no_secrets(cache.endpoint, "SchemaCache.endpoint")

    cache_file = cache.cache_path(LEAKY_URL)
    assert_no_secrets(cache_file.name, "cache filename")
    assert_no_secrets(cache_file.read_text(), "cache file contents")

    report = await cache.refresh()
    assert_no_secrets(json.dumps(report), "refresh() report")

    # The transport really was handed the credentials, so this is a genuine test.
    assert any(API_KEY in json.dumps(h) for h in transport.headers_seen)

    # --- failure path, which is where the raw URL used to escape.
    # httpx and gql error strings routinely embed the request URL, so simulate
    # a transport message that does exactly that.
    transport.fail_message = f"Connection refused for {LEAKY_URL}"
    with pytest.raises(GraphQLSchemaError) as exc:
        await cache.refresh()
    assert_no_secrets(str(exc.value), "GraphQLSchemaError built from a transport message")
    assert_no_secrets(cache.endpoint, "SchemaCache.endpoint after failure")

    # Same for GraphQL errors returned in a 200.
    transport.fail_message = None
    transport.response = {"errors": [{"message": f"unauthorized for {LEAKY_URL}"}]}
    with pytest.raises(GraphQLSchemaError) as exc:
        await cache.refresh()
    assert_no_secrets(str(exc.value), "GraphQLSchemaError built from GraphQL errors")

    # --- everything that was logged along the way
    assert_no_secrets("\n".join(captured_logs), "log records")
    assert len(captured_logs) > 0, "no logs captured, so the assertion proved nothing"


async def test_refresh_schema_tool_output_is_credential_free(
    cache_dir, transport, monkeypatch
):
    from src.text_to_graphql_mcp import mcp_server

    monkeypatch.setattr("src.text_to_graphql_mcp.config.settings.GRAPHQL_API_KEY", API_KEY)
    cache = SchemaCache(
        cache_dir=cache_dir, transport=transport, ttl_seconds=3600, endpoint=LEAKY_URL
    )
    monkeypatch.setattr(mcp_server, "schema_cache", cache)

    assert_no_secrets(await mcp_server.refresh_schema(), "refresh_schema success output")

    transport.fail_message = "Connection refused"
    assert_no_secrets(await mcp_server.refresh_schema(), "refresh_schema failure output")


def test_redact_url_handles_the_shapes_we_care_about():
    assert redact_url("https://u:p@host/graphql") == "https://host/graphql"
    assert redact_url("https://host/graphql?token=abc") == "https://host/graphql"
    assert redact_url("https://host:8080/graphql") == "https://host:8080/graphql"
    assert redact_url("https://host/graphql#frag") == "https://host/graphql"
    # Never echoes back something it could not parse safely.
    assert redact_url("not-a-url") == "<unparseable url>"
    assert redact_url("") == ""
    assert redact_url(None) == ""
    assert "p" not in redact_url("https://u:p@host/")[8:]


def test_graphql_helpers_logs_a_redacted_endpoint(captured_logs):
    from src.text_to_graphql_mcp.tools.graphql_helpers import execute_graphql_query

    execute_graphql_query(
        f"https://svc:{PASSWORD}@127.0.0.1:1/graphql",
        "query { __typename }",
        {"Content-Type": "application/json"},
        timeout=2,
    )

    joined = "\n".join(captured_logs)
    assert "127.0.0.1:1/graphql" in joined, "endpoint should still be logged for debugging"
    assert PASSWORD not in joined
