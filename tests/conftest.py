"""
Shared test fixtures.

Settings snapshot the environment at import time (config.py builds its field
defaults with os.getenv), and importing mcp_server constructs the agent and its
LLM client, so these must be set before the package is imported anywhere.
"""

import json
import os
import tempfile
import threading

# Must precede any import of the package under test.
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("GRAPHQL_ENDPOINT", "https://example.test/graphql")
os.environ.setdefault("GRAPHQL_API_KEY", "test-graphql-key")
# Keep the process-level default cache away from the developer's real ~/.cache.
os.environ.setdefault("SCHEMA_CACHE_DIR", tempfile.mkdtemp(prefix="t2g-test-cache-"))

import pytest  # noqa: E402
from graphql import build_schema, get_introspection_query, graphql_sync  # noqa: E402

from src.text_to_graphql_mcp.schema_cache import (  # noqa: E402
    CACHE_VERSION,
    SchemaCache,
    cache_filename,
)

TEST_ENDPOINT = "https://example.test/graphql"

SDL_V1 = """
type Query {
  hello: String
  user(id: ID!): User
}

type User {
  id: ID!
  name: String
}
"""

SDL_V2 = """
type Query {
  hello: String
  user(id: ID!): User
}

type User {
  id: ID!
  name: String
  email: String
}
"""


def introspect_locally(sdl, query=None):
    """Run a real introspection query against a locally built schema."""
    schema = build_schema(sdl)
    result = graphql_sync(schema, query or get_introspection_query(descriptions=True))
    assert not result.errors, result.errors
    return result.data


class FakeTransport:
    """
    Stand-in for execute_graphql_query. Records calls and never touches the
    network. Defaults to answering the introspection query honestly, from a
    locally built schema.
    """

    def __init__(self, sdl=SDL_V1):
        self.sdl = sdl
        self.calls = 0
        self.headers_seen = []
        self.queries_seen = []
        self.response = None       # override the entire response
        self.fail_message = None   # simulate execute_graphql_query's failure envelope
        self.raise_error = None    # simulate an exception escaping the transport
        self.gate = None           # threading.Event to block inside the call
        self.started = threading.Event()  # set as soon as a call is in flight

    def __call__(self, endpoint_url, query, headers):
        self.calls += 1
        self.headers_seen.append(headers)
        self.queries_seen.append(query)
        self.started.set()

        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.raise_error is not None:
            raise self.raise_error
        if self.fail_message is not None:
            return {
                "success": False,
                "message": self.fail_message,
                "exception": "TransportQueryError",
            }
        if self.response is not None:
            return self.response
        return {"success": True, "data": introspect_locally(self.sdl, query)}


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "schemas"


@pytest.fixture
def cache(cache_dir, transport):
    """An isolated SchemaCache: own directory, own transport, own lock."""
    return SchemaCache(
        cache_dir=cache_dir,
        transport=transport,
        ttl_seconds=3600,
        endpoint=TEST_ENDPOINT,
    )


@pytest.fixture
def cache_path(cache_dir):
    def _path(endpoint=TEST_ENDPOINT):
        return cache_dir / cache_filename(endpoint)

    return _path


@pytest.fixture
def write_envelope(cache_path):
    """Write a cache file directly, so tests can control refreshed_at."""

    def _write(
        refreshed_at,
        endpoint=TEST_ENDPOINT,
        sdl=SDL_V1,
        schema_hash="sha256:previous",
        cache_version=CACHE_VERSION,
        introspection=None,
        raw=None,
    ):
        path = cache_path(endpoint)
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return path
        envelope = {
            "cache_version": cache_version,
            "endpoint": endpoint,
            "refreshed_at": refreshed_at,
            "schema_hash": schema_hash,
            "introspection": introspection
            if introspection is not None
            else introspect_locally(sdl),
        }
        path.write_text(json.dumps(envelope), encoding="utf-8")
        return path

    return _write
