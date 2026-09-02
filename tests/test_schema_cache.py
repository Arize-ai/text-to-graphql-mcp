"""
Tests for the GraphQL schema cache lifecycle.

Every network interaction is mocked; nothing here depends on a live API.
"""

import asyncio
import json
import threading
import time

import pytest
from graphql import GraphQLSchema

from src.text_to_graphql_mcp.exceptions import GraphQLSchemaError
from src.text_to_graphql_mcp.schema_cache import (
    CACHE_VERSION,
    SOURCE_FRESH_CACHE,
    SOURCE_LIVE,
    SOURCE_MEMORY,
    SOURCE_STALE_CACHE,
    SchemaCache,
    cache_filename,
    normalize_endpoint,
)

from .conftest import SDL_V1, SDL_V2, TEST_ENDPOINT, introspect_locally

NOW = time.time
HOUR = 3600


# --- 1. no cache -> live introspection --------------------------------------

async def test_no_cache_introspects_live(cache, transport, cache_path):
    loaded = await cache.get_schema()

    assert loaded.source == SOURCE_LIVE
    assert isinstance(loaded.schema, GraphQLSchema)
    assert loaded.schema.query_type.name == "Query"
    assert transport.calls == 1
    assert cache_path().exists()

    envelope = json.loads(cache_path().read_text())
    assert envelope["cache_version"] == CACHE_VERSION
    assert "__schema" in envelope["introspection"]
    assert envelope["schema_hash"] == loaded.schema_hash


async def test_cache_file_holds_no_credentials(cache, transport, cache_path):
    await cache.get_schema()

    # The transport is handed real auth headers ...
    assert any("Authorization" in h or "X-API-Key" in h for h in transport.headers_seen)
    # ... but none of that may reach disk.
    contents = cache_path().read_text()
    assert "test-graphql-key" not in contents
    assert "Authorization" not in contents
    assert "X-API-Key" not in contents


# --- 2. fresh disk cache -> no introspection --------------------------------

async def test_fresh_disk_cache_avoids_introspection(cache, transport, write_envelope):
    write_envelope(refreshed_at=NOW())

    loaded = await cache.get_schema()

    assert loaded.source == SOURCE_FRESH_CACHE
    assert transport.calls == 0
    assert loaded.schema_hash == "sha256:previous"  # hash carried from the envelope


# --- 3 & 18. fresh memory -> no disk, no network, no rebuild ----------------

async def test_memory_hit_touches_neither_disk_nor_network(cache, transport, cache_path):
    first = await cache.get_schema()
    assert transport.calls == 1

    # Remove the cache file: a memory hit must not need it.
    cache_path().unlink()

    second = await cache.get_schema()

    assert second.source == SOURCE_MEMORY
    assert transport.calls == 1
    # Same object, not a rebuilt copy -- proves build_client_schema did not re-run.
    assert second.schema is first.schema
    assert second.schema_hash == first.schema_hash


# --- 4. expired cache -> live refresh ---------------------------------------

async def test_expired_cache_refreshes_live(cache, transport, write_envelope):
    write_envelope(refreshed_at=NOW() - 2 * HOUR)

    loaded = await cache.get_schema()

    assert loaded.source == SOURCE_LIVE
    assert transport.calls == 1


async def test_expired_memory_refreshes_live(cache_dir, transport):
    short = SchemaCache(
        cache_dir=cache_dir, transport=transport, ttl_seconds=0, endpoint=TEST_ENDPOINT
    )
    await short.get_schema()
    await short.get_schema()

    # TTL of zero means every call is stale, so no memory or disk hit is possible.
    assert transport.calls == 2


# --- 5. expired cache + network failure -> stale fallback -------------------

async def test_network_failure_falls_back_to_stale_cache(cache, transport, write_envelope):
    write_envelope(refreshed_at=NOW() - 2 * HOUR)
    transport.fail_message = "Client setup error: connection refused"

    loaded = await cache.get_schema()

    assert loaded.source == SOURCE_STALE_CACHE
    assert transport.calls == len(transport.queries_seen) >= 1
    assert loaded.schema.query_type.name == "Query"


async def test_no_live_and_no_cache_raises(cache, transport):
    transport.fail_message = "connection refused"

    with pytest.raises(GraphQLSchemaError) as exc:
        await cache.get_schema()

    assert "introspection failed" in str(exc.value).lower()


# --- 6, 13, 14, 16. invalid introspection must never overwrite the cache ----

@pytest.mark.parametrize(
    "response,fail_message,label",
    [
        ({"success": True, "data": {"__schema": {"types": "not-a-list"}}}, None, "malformed schema"),
        ({"data": None, "errors": [{"message": "unauthorized"}]}, None, "graphql errors in a 200"),
        ({"success": True, "data": {"queryType": {"name": "Query"}}}, None, "missing __schema"),
        (None, "Client setup error: boom", "transport failure envelope"),
    ],
)
async def test_invalid_introspection_preserves_cache(
    cache, transport, write_envelope, cache_path, response, fail_message, label
):
    write_envelope(refreshed_at=NOW() - 2 * HOUR)
    before = cache_path().read_bytes()

    transport.response = response
    transport.fail_message = fail_message

    loaded = await cache.get_schema()

    # Served from the last known-good cache, and that cache is byte-identical.
    assert loaded.source == SOURCE_STALE_CACHE, label
    assert cache_path().read_bytes() == before, label


async def test_invalid_introspection_with_no_cache_raises(cache, transport, cache_path):
    transport.response = {"success": True, "data": {"__schema": {"types": "not-a-list"}}}

    with pytest.raises(GraphQLSchemaError):
        await cache.get_schema()

    assert not cache_path().exists()


# --- 7. forced refresh ignores the TTL --------------------------------------

async def test_forced_refresh_ignores_ttl(cache, transport, write_envelope):
    write_envelope(refreshed_at=NOW())  # perfectly fresh

    loaded = await cache.get_schema(force=True)

    assert loaded.source == SOURCE_LIVE
    assert transport.calls == 1


async def test_forced_refresh_ignores_fresh_memory(cache, transport):
    await cache.get_schema()
    assert transport.calls == 1

    loaded = await cache.get_schema(force=True)

    assert loaded.source == SOURCE_LIVE
    assert transport.calls == 2


# --- 8. forced refresh failure preserves the current schema -----------------

async def test_forced_refresh_failure_preserves_current_schema(
    cache, transport, cache_path
):
    original = await cache.get_schema()
    before = cache_path().read_bytes()

    transport.fail_message = "connection refused"

    with pytest.raises(GraphQLSchemaError):
        await cache.refresh()

    # An explicit refresh reports failure rather than quietly serving stale, but
    # the cache it failed to replace is untouched and still in use.
    assert cache.current().schema is original.schema
    assert cache_path().read_bytes() == before

    transport.fail_message = None
    still_usable = await cache.get_schema()
    assert still_usable.source == SOURCE_MEMORY
    assert still_usable.schema is original.schema


# --- 9. concurrent refresh at TTL expiry -> exactly one introspection -------

async def test_concurrent_expiry_introspects_once(cache, transport, write_envelope):
    write_envelope(refreshed_at=NOW() - 2 * HOUR)

    release = threading.Event()
    transport.gate = release

    first = asyncio.create_task(cache.get_schema())
    second = asyncio.create_task(cache.get_schema())

    # Wait until one introspection is genuinely in flight (running in a worker
    # thread), which means the other task is parked on the refresh lock.
    deadline = time.monotonic() + 5
    while not transport.started.is_set():
        assert time.monotonic() < deadline, "introspection never started"
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    release.set()

    a, b = await asyncio.gather(first, second)

    assert transport.calls == 1
    # The loser of the race re-checked the cache after acquiring the lock.
    assert {a.source, b.source} <= {SOURCE_LIVE, SOURCE_MEMORY}
    assert a.schema is b.schema


# --- 10. cache identity -----------------------------------------------------

def test_paths_on_one_host_get_distinct_cache_keys():
    a = cache_filename("https://example.com/graphql")
    b = cache_filename("https://example.com/v2/graphql")

    assert a != b
    assert a.startswith("example-com-") and b.startswith("example-com-")


def test_endpoint_normalization_is_stable_and_credential_free():
    assert normalize_endpoint("HTTPS://Example.COM:443/graphql/") == "https://example.com/graphql"
    assert cache_filename("https://example.com/graphql") == cache_filename(
        "https://EXAMPLE.com:443/graphql/"
    )

    # userinfo, query and fragment are all dropped
    assert "secret" not in normalize_endpoint("https://user:secret@example.com/graphql")
    assert "token" not in cache_filename("https://example.com/graphql?token=abc123")

    with pytest.raises(ValueError):
        normalize_endpoint("not-a-url")


def test_distinct_endpoints_do_not_share_memory(cache_dir, transport):
    one = SchemaCache(cache_dir=cache_dir, transport=transport, endpoint="https://a.test/graphql")
    assert one.cache_path("https://a.test/graphql") != one.cache_path("https://b.test/graphql")


# --- 11. a failed atomic replace cannot corrupt the previous cache ----------

async def test_failed_replace_leaves_previous_cache_byte_identical(
    cache, transport, cache_dir, cache_path
):
    await cache.get_schema()
    before = cache_path().read_bytes()

    def exploding_replace(tmp_path, path):
        raise OSError("simulated failure between write and rename")

    cache._replace = exploding_replace
    cache.invalidate()
    transport.sdl = SDL_V2

    # The refresh itself still succeeds; only the persist step fails.
    loaded = await cache.get_schema(force=True)
    assert loaded.source == SOURCE_LIVE
    assert "email" in loaded.schema.get_type("User").fields

    assert cache_path().read_bytes() == before
    # No half-written temp file left behind.
    assert [p.name for p in cache_dir.iterdir()] == [cache_path().name]


# --- 12. changed vs unchanged reporting -------------------------------------

async def test_refresh_reports_unchanged(cache, transport):
    first = await cache.get_schema()

    report = await cache.refresh()

    assert report["changed"] is False
    assert report["previous_hash"] == first.schema_hash
    assert report["schema_hash"] == first.schema_hash


async def test_refresh_reports_changed(cache, transport):
    first = await cache.get_schema()
    transport.sdl = SDL_V2

    report = await cache.refresh()

    assert report["changed"] is True
    assert report["previous_hash"] == first.schema_hash
    assert report["schema_hash"] != first.schema_hash


async def test_refresh_reports_previous_hash_from_disk_after_restart(
    cache_dir, transport, write_envelope
):
    # Simulate a fresh process: disk cache present, memory empty.
    write_envelope(refreshed_at=NOW() - 2 * HOUR, schema_hash="sha256:from-disk")
    fresh_process = SchemaCache(
        cache_dir=cache_dir, transport=transport, ttl_seconds=3600, endpoint=TEST_ENDPOINT
    )

    report = await fresh_process.refresh()

    assert report["previous_hash"] == "sha256:from-disk"
    assert report["changed"] is True


# --- 15. a pre-existing poisoned cache file is rejected, not served ---------

async def test_legacy_error_payload_cache_is_rejected(
    cache, transport, write_envelope, cache_path
):
    # Exactly what the old code persisted when introspection failed: the
    # transport's failure envelope, written as if it were a schema.
    write_envelope(
        refreshed_at=NOW(),
        raw=json.dumps(
            {
                "success": False,
                "message": "Client setup error: nodename nor servname provided",
                "exception": "ConnectError",
            }
        ),
    )

    loaded = await cache.get_schema()

    assert loaded.source == SOURCE_LIVE
    assert transport.calls == 1
    # The bad file has been replaced by a real envelope.
    assert json.loads(cache_path().read_text())["cache_version"] == CACHE_VERSION


@pytest.mark.parametrize(
    "raw,label",
    [
        ("{ not json", "corrupt json"),
        (json.dumps({"cache_version": 999, "introspection": {"__schema": {}}}), "future version"),
        (json.dumps({"cache_version": CACHE_VERSION, "refreshed_at": 0}), "no introspection"),
        (json.dumps({"cache_version": CACHE_VERSION, "introspection": {"__schema": {}}}), "no timestamp"),
        (json.dumps([1, 2, 3]), "not an object"),
    ],
)
async def test_unusable_cache_files_are_ignored(cache, transport, write_envelope, raw, label):
    write_envelope(refreshed_at=NOW(), raw=raw)

    loaded = await cache.get_schema()

    assert loaded.source == SOURCE_LIVE, label
    assert transport.calls == 1, label


# --- 17. unconfigured endpoint ----------------------------------------------

async def test_missing_endpoint_raises_clear_error(cache_dir, transport):
    unconfigured = SchemaCache(cache_dir=cache_dir, transport=transport, endpoint="")

    with pytest.raises(GraphQLSchemaError) as exc:
        await unconfigured.get_schema()

    assert "GRAPHQL_ENDPOINT" in str(exc.value)
    assert transport.calls == 0
    assert not cache_dir.exists()


# --- 19. unwritable cache directory degrades to memory-only -----------------

async def test_unwritable_cache_dir_degrades_to_memory(tmp_path, transport):
    # A regular file where the cache directory should be, so mkdir fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    cache = SchemaCache(
        cache_dir=blocker / "schemas",
        transport=transport,
        ttl_seconds=3600,
        endpoint=TEST_ENDPOINT,
    )

    loaded = await cache.get_schema()
    assert loaded.source == SOURCE_LIVE

    # Still cached in memory, so no second introspection.
    again = await cache.get_schema()
    assert again.source == SOURCE_MEMORY
    assert transport.calls == 1


# --- introspection query construction ---------------------------------------

async def test_uses_graphql_core_introspection_query(cache, transport):
    await cache.get_schema()

    query = transport.queries_seen[0]
    assert "IntrospectionQuery" in query
    # Optional meta-fields stay off: a server that does not implement them
    # rejects the entire query with a validation error.
    for risky in ("specifiedByURL", "specifiedByUrl", "isRepeatable", "isOneOf"):
        assert risky not in query


async def test_falls_back_to_a_descriptions_free_query(cache, transport):
    calls = {"n": 0}
    real = introspect_locally

    def picky(endpoint_url, query, headers):
        calls["n"] += 1
        transport.queries_seen.append(query)
        if "description" in query:
            return {
                "data": None,
                "errors": [{"message": 'Cannot query field "description" on type "__Type".'}],
            }
        return {"success": True, "data": real(SDL_V1, query)}

    cache._transport = picky

    loaded = await cache.get_schema()

    assert loaded.source == SOURCE_LIVE
    assert calls["n"] == 2
    assert "description" not in transport.queries_seen[-1]


# --- the refresh_schema MCP tool --------------------------------------------

async def test_refresh_schema_tool_reports_changed(monkeypatch, cache, transport):
    from src.text_to_graphql_mcp import mcp_server

    monkeypatch.setattr(mcp_server, "schema_cache", cache)
    await cache.get_schema()
    transport.sdl = SDL_V2

    payload = json.loads(await mcp_server.refresh_schema())

    assert payload["refreshed"] is True
    assert payload["changed"] is True
    assert payload["message"].endswith("Changed: yes")
    assert payload["previous_hash"] != payload["current_hash"]
    assert "test-graphql-key" not in json.dumps(payload)


async def test_refresh_schema_tool_reports_unchanged(monkeypatch, cache, transport):
    from src.text_to_graphql_mcp import mcp_server

    monkeypatch.setattr(mcp_server, "schema_cache", cache)
    await cache.get_schema()

    payload = json.loads(await mcp_server.refresh_schema())

    assert payload["refreshed"] is True
    assert payload["changed"] is False
    assert payload["message"].endswith("Changed: no")
    assert payload["previous_hash"] == payload["current_hash"]


async def test_refresh_schema_tool_failure_retains_previous(monkeypatch, cache, transport):
    from src.text_to_graphql_mcp import mcp_server

    monkeypatch.setattr(mcp_server, "schema_cache", cache)
    original = await cache.get_schema()
    transport.fail_message = "connection refused"

    payload = json.loads(await mcp_server.refresh_schema())

    assert payload["refreshed"] is False
    assert "retained" in payload["message"]
    assert payload["current_hash"] == original.schema_hash
    # And the schema still in use is the original object.
    assert cache.current().schema is original.schema


async def test_refresh_schema_tool_is_documented_as_an_escape_hatch():
    from src.text_to_graphql_mcp import mcp_server

    doc = mcp_server.refresh_schema.__doc__ or ""
    assert "ESCAPE HATCH" in doc
    assert "do not call this before normal queries" in doc.lower()


# --- credential hygiene ------------------------------------------------------

async def test_basic_auth_in_the_endpoint_never_leaves_the_request(cache_dir, transport):
    """A https://user:pass@host/graphql config must not leak the password into
    logs, the cache file, or the refresh_schema output."""
    secret = "s3cr3t-password"
    raw_url = f"https://svc:{secret}@api.example.test/graphql"

    leaky = SchemaCache(
        cache_dir=cache_dir, transport=transport, ttl_seconds=3600, endpoint=raw_url
    )
    loaded = await leaky.get_schema()

    # The request itself still goes to the full URL ...
    assert transport.calls == 1
    # ... but nothing reported or persisted carries the credential.
    assert secret not in loaded.endpoint
    assert secret not in json.dumps(loaded.describe())
    assert loaded.endpoint == "https://api.example.test/graphql"

    report = await leaky.refresh()
    assert secret not in json.dumps(report)

    cache_file = leaky.cache_path(raw_url)
    assert secret not in cache_file.read_text()
    assert secret not in cache_file.name


async def test_unparseable_endpoint_raises_instead_of_silently_not_caching(
    cache_dir, transport
):
    broken = SchemaCache(cache_dir=cache_dir, transport=transport, endpoint="not-a-url")

    with pytest.raises(GraphQLSchemaError) as exc:
        await broken.get_schema()

    assert "not a usable URL" in str(exc.value)
    assert transport.calls == 0


async def test_introspection_request_is_time_bounded(monkeypatch):
    """Introspection holds the refresh lock, so the request must be bounded."""
    import src.text_to_graphql_mcp.schema_cache as sc

    seen = {}

    def fake_execute(endpoint_url, query, headers, timeout=None):
        seen["timeout"] = timeout
        return {"success": True, "data": introspect_locally(SDL_V1, query)}

    import src.text_to_graphql_mcp.tools.graphql_helpers as helpers

    monkeypatch.setattr(helpers, "execute_graphql_query", fake_execute)

    sc._default_transport("https://example.test/graphql", "query { __typename }", {})

    assert isinstance(seen["timeout"], int)
    assert seen["timeout"] > 0
