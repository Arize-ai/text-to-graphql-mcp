"""
GraphQL schema cache for the Text-to-GraphQL MCP Server.

Owns the whole schema lifecycle: an in-memory schema guarded by a TTL, a
last-known-good disk cache written atomically, live introspection, and an
explicit forced refresh.

Lookup order (see ``SchemaCache.get_schema``):

    memory (within TTL) -> fresh disk cache -> live introspection -> stale disk
    cache -> error

Nothing is ever persisted or served until it has been proven to build into a
real ``GraphQLSchema``, so a failed or malformed introspection can neither
destroy nor impersonate the previous usable cache.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from graphql import GraphQLSchema, build_client_schema, get_introspection_query, print_schema

from .config import settings
from .exceptions import GraphQLSchemaError
from .logger import logger

# Bumped whenever the on-disk envelope layout changes. Files without a matching
# version are ignored rather than migrated -- pre-1 files were CWD-relative and
# could contain a persisted *error* payload rather than a schema.
CACHE_VERSION = 1

DEFAULT_TTL_SECONDS = 3600

SOURCE_MEMORY = "memory"
SOURCE_LIVE = "live"
SOURCE_FRESH_CACHE = "fresh_cache"
SOURCE_STALE_CACHE = "stale_cache"

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNSAFE_FILENAME = re.compile(r"[^a-z0-9]+")


@dataclass
class LoadedSchema:
    """A validated schema plus the provenance needed to debug it."""

    schema: GraphQLSchema
    source: str
    endpoint: str
    refreshed_at: float
    schema_hash: str

    @property
    def cache_age(self) -> float:
        return max(0.0, time.time() - self.refreshed_at)

    def describe(self) -> Dict[str, Any]:
        """Observability payload. Deliberately carries no credentials or headers."""
        return {
            "source": self.source,
            "endpoint": self.endpoint,
            "refreshed_at": round(self.refreshed_at, 3),
            "cache_age": round(self.cache_age, 3),
            "schema_hash": self.schema_hash,
        }


def normalize_endpoint(endpoint_url: str) -> str:
    """
    Canonicalize an endpoint URL for use as a cache identity.

    Keeps the path, so ``example.com/graphql`` and ``example.com/v2/graphql``
    cannot collide. Drops userinfo, query and fragment, so no credential
    material can reach a cache filename.
    """
    parts = urlsplit((endpoint_url or "").strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"Endpoint URL has no host: {endpoint_url!r}")

    netloc = host
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Endpoint URL has an invalid port: {endpoint_url!r}") from exc
    if port is not None and _DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{host}:{port}"

    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def cache_filename(endpoint_url: str) -> str:
    """Readable host plus a short hash of the *full* normalized URL."""
    normalized = normalize_endpoint(endpoint_url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    host = urlsplit(normalized).hostname or "endpoint"
    readable = _UNSAFE_FILENAME.sub("-", host).strip("-") or "endpoint"
    return f"{readable}-{digest}.json"


def default_cache_dir() -> Optional[Path]:
    """
    SCHEMA_CACHE_DIR -> $XDG_CACHE_HOME -> ~/.cache.

    Returns None when no location can be determined (e.g. no HOME), which puts
    the cache in memory-only mode rather than failing.
    """
    configured = (getattr(settings, "SCHEMA_CACHE_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser()

    xdg = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    try:
        base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    except RuntimeError:  # home directory could not be resolved
        return None
    return base / "text-to-graphql-mcp" / "schemas"


def schema_fingerprint(schema: GraphQLSchema) -> str:
    """
    Hash the printed SDL, not the introspection JSON.

    Introspection field ordering is not guaranteed stable between responses, so
    hashing the raw JSON would report spurious changes.
    """
    return "sha256:" + hashlib.sha256(print_schema(schema).encode("utf-8")).hexdigest()


def _default_transport(endpoint_url: str, query: str, headers: Dict[str, str]) -> Any:
    # Imported lazily: tools/__init__ pulls in schema_management, which imports
    # this module, so a top-level import would be circular.
    from .tools.graphql_helpers import execute_graphql_query

    return execute_graphql_query(endpoint_url, query, headers)


def extract_introspection(raw: Any) -> Dict[str, Any]:
    """
    Pull the introspection payload out of a transport response, or raise.

    This is the gate that stops a failure envelope from being mistaken for a
    schema. ``execute_graphql_query`` never returns None -- it returns
    ``{"success": False, ...}`` -- and a 200 response can still carry
    ``errors``, so both shapes are rejected explicitly.
    """
    if raw is None:
        raise GraphQLSchemaError("Introspection returned no response", schema_source="live")
    if not isinstance(raw, dict):
        raise GraphQLSchemaError(
            f"Introspection returned {type(raw).__name__}, expected an object",
            schema_source="live",
        )
    if raw.get("success") is False:
        raise GraphQLSchemaError(
            f"Introspection request failed: {raw.get('message') or 'unknown transport error'}",
            schema_source="live",
        )
    if raw.get("errors"):
        raise GraphQLSchemaError(
            f"Introspection response carried GraphQL errors: {json.dumps(raw['errors'])[:500]}",
            schema_source="live",
        )

    payload = raw.get("data", raw)
    if not isinstance(payload, dict) or "__schema" not in payload:
        raise GraphQLSchemaError(
            "Introspection response is missing '__schema'",
            schema_source="live",
        )
    return payload


def build_validated_schema(payload: Dict[str, Any], source: str) -> GraphQLSchema:
    """Build a client schema, turning any failure into a hard error."""
    try:
        return build_client_schema(payload)  # type: ignore[arg-type]
    except Exception as exc:
        raise GraphQLSchemaError(
            f"Introspection payload did not produce a valid schema: {exc}",
            schema_source=source,
        ) from exc


class SchemaCache:
    """
    Process-level schema cache.

    A class rather than module globals so tests can construct an isolated
    instance with its own cache directory, transport and lock.
    """

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        transport: Optional[Callable[[str, str, Dict[str, str]], Any]] = None,
        ttl_seconds: Optional[int] = None,
        endpoint: Optional[str] = None,
    ):
        self._cache_dir_override = Path(cache_dir) if cache_dir is not None else None
        self._transport = transport or _default_transport
        self._ttl_override = ttl_seconds
        self._endpoint_override = endpoint

        self._memory: Optional[LoadedSchema] = None
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._warned_unwritable = False

    # ---- configuration -----------------------------------------------------

    def _endpoint(self) -> str:
        if self._endpoint_override is not None:
            return self._endpoint_override.strip()
        return (settings.GRAPHQL_ENDPOINT or "").strip()

    def _ttl(self) -> int:
        if self._ttl_override is not None:
            return max(0, int(self._ttl_override))
        return max(0, int(getattr(settings, "SCHEMA_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS)))

    def _cache_dir(self) -> Optional[Path]:
        if self._cache_dir_override is not None:
            return self._cache_dir_override
        return default_cache_dir()

    def cache_path(self, endpoint_url: str) -> Optional[Path]:
        directory = self._cache_dir()
        if directory is None:
            return None
        try:
            return directory / cache_filename(endpoint_url)
        except ValueError:
            return None

    def _lock_for_loop(self) -> asyncio.Lock:
        # A lock created at import time binds to whatever loop exists then, which
        # breaks across the separate event loops pytest creates per test.
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    # ---- disk --------------------------------------------------------------

    def _read_envelope(self, path: Optional[Path]) -> Optional[Dict[str, Any]]:
        """Read and structurally validate a cache envelope. None if unusable."""
        if path is None:
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.json("WARNING", "Could not read schema cache file", {
                "path": str(path),
                "error": str(exc),
            })
            return None

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.json("WARNING", "Ignoring corrupt schema cache file", {
                "path": str(path),
                "error": str(exc),
            })
            return None

        if not isinstance(envelope, dict) or envelope.get("cache_version") != CACHE_VERSION:
            logger.json("WARNING", "Ignoring schema cache file with unrecognized layout", {
                "path": str(path),
                "cache_version": envelope.get("cache_version") if isinstance(envelope, dict) else None,
            })
            return None

        introspection = envelope.get("introspection")
        if not isinstance(introspection, dict) or "__schema" not in introspection:
            logger.json("WARNING", "Ignoring schema cache file with no introspection payload", {
                "path": str(path),
            })
            return None

        if not isinstance(envelope.get("refreshed_at"), (int, float)):
            logger.json("WARNING", "Ignoring schema cache file with no usable timestamp", {
                "path": str(path),
            })
            return None

        return envelope

    def _replace(self, tmp_path: str, path: Path) -> None:
        """Seam for tests: the atomic rename step."""
        os.replace(tmp_path, path)

    def _write_envelope(self, path: Path, envelope: Dict[str, Any]) -> None:
        """
        Write atomically: temp file in the *same* directory (os.replace is only
        atomic within one filesystem), fsync, then rename over the target.
        """
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)

        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(directory), prefix=f"{path.name}.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _persist(self, endpoint: str, payload: Dict[str, Any], loaded: LoadedSchema) -> None:
        """Best-effort cache write. Never fails the caller."""
        path = self.cache_path(endpoint)
        if path is None:
            return
        envelope = {
            "cache_version": CACHE_VERSION,
            "endpoint": normalize_endpoint(endpoint),
            "refreshed_at": loaded.refreshed_at,
            "schema_hash": loaded.schema_hash,
            "introspection": payload,
        }
        try:
            self._write_envelope(path, envelope)
        except OSError as exc:
            if not self._warned_unwritable:
                self._warned_unwritable = True
                logger.json("WARNING", "Schema cache directory is not writable; continuing in memory only", {
                    "path": str(path),
                    "error": str(exc),
                })
            return
        logger.json("INFO", "Schema cache updated", {
            "path": str(path),
            "schema_hash": loaded.schema_hash,
        })

    def stored_hash(self, endpoint: str) -> Optional[str]:
        envelope = self._read_envelope(self.cache_path(endpoint))
        if not envelope:
            return None
        stored = envelope.get("schema_hash")
        return stored if isinstance(stored, str) else None

    # ---- loading -----------------------------------------------------------

    def _is_fresh(self, refreshed_at: float) -> bool:
        return (time.time() - refreshed_at) < self._ttl()

    def _from_envelope(self, envelope: Dict[str, Any], endpoint: str, source: str) -> Optional[LoadedSchema]:
        try:
            schema = build_validated_schema(envelope["introspection"], source)
        except GraphQLSchemaError as exc:
            logger.json("WARNING", "Cached introspection payload no longer builds; ignoring it", {
                "endpoint": endpoint,
                "source": source,
                "error": str(exc),
            })
            return None

        stored = envelope.get("schema_hash")
        return LoadedSchema(
            schema=schema,
            source=source,
            endpoint=endpoint,
            refreshed_at=float(envelope["refreshed_at"]),
            schema_hash=stored if isinstance(stored, str) else schema_fingerprint(schema),
        )

    def _query_variants(self) -> list[Dict[str, Any]]:
        """
        Conservative introspection first, then a descriptions-free retry.

        Every optional meta-field stays off: a server that does not implement
        ``specifiedByURL``, ``isRepeatable``, input-value deprecation or
        ``isOneOf`` rejects the *entire* query with a validation error, which
        would take introspection down altogether.
        """
        base = {
            "descriptions": True,
            "specified_by_url": False,
            "directive_is_repeatable": False,
            "schema_description": False,
            "input_value_deprecation": False,
            "input_object_one_of": False,
        }
        return [base, {**base, "descriptions": False}]

    async def _introspect(self, endpoint: str) -> LoadedSchema:
        headers = settings.get_graphql_headers()
        last_error: Optional[Exception] = None

        for variant in self._query_variants():
            query = get_introspection_query(**variant)
            try:
                # The gql transport is synchronous; keep it off the event loop.
                raw = await asyncio.to_thread(self._transport, endpoint, query, headers)
                payload = extract_introspection(raw)
                schema = build_validated_schema(payload, SOURCE_LIVE)
            except Exception as exc:
                last_error = exc
                logger.json("WARNING", "Introspection attempt failed", {
                    "endpoint": endpoint,
                    "descriptions": variant["descriptions"],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                continue

            loaded = LoadedSchema(
                schema=schema,
                source=SOURCE_LIVE,
                endpoint=endpoint,
                refreshed_at=time.time(),
                schema_hash=schema_fingerprint(schema),
            )
            # Only now, with a validated schema in hand, is the cache touched.
            self._persist(endpoint, payload, loaded)
            return loaded

        raise GraphQLSchemaError(
            f"Live introspection failed for {endpoint}: {last_error}",
            schema_source="live",
        ) from last_error

    async def get_schema(self, force: bool = False) -> LoadedSchema:
        """
        Return a usable schema, introspecting only when stale, missing or forced.

        Falls back to the last known-good stale cache when live introspection
        fails, so a refresh failure alone never fails a request.
        """
        return await self._resolve(force=force, allow_stale=True)

    async def refresh(self) -> Dict[str, Any]:
        """
        Forced refresh. Bypasses the TTL and does *not* accept a stale fallback:
        an explicit refresh that could not reach a live schema is a failure, and
        the existing memory and disk cache are left untouched.
        """
        endpoint = self._endpoint()
        previous_hash = self._memory.schema_hash if self._memory else (
            self.stored_hash(endpoint) if endpoint else None
        )

        loaded = await self._resolve(force=True, allow_stale=False)

        report = loaded.describe()
        report["previous_hash"] = previous_hash
        report["changed"] = previous_hash != loaded.schema_hash
        return report

    async def _resolve(self, force: bool, allow_stale: bool) -> LoadedSchema:
        endpoint = self._endpoint()
        if not endpoint:
            raise GraphQLSchemaError(
                "No GraphQL endpoint configured. Set GRAPHQL_ENDPOINT to introspect a schema.",
                schema_source="config",
            )

        if not force:
            hit = self._memory_hit(endpoint)
            if hit is not None:
                return hit

        async with self._lock_for_loop():
            # Re-check under the lock: another task may have just refreshed.
            if not force:
                hit = self._memory_hit(endpoint)
                if hit is not None:
                    return hit

            path = self.cache_path(endpoint)
            envelope = self._read_envelope(path)

            if envelope and not force and self._is_fresh(float(envelope["refreshed_at"])):
                loaded = self._from_envelope(envelope, endpoint, SOURCE_FRESH_CACHE)
                if loaded is not None:
                    self._memory = loaded
                    logger.json("INFO", "Schema loaded from disk cache", loaded.describe())
                    return loaded

            try:
                loaded = await self._introspect(endpoint)
            except Exception as live_error:
                if allow_stale and envelope:
                    stale = self._from_envelope(envelope, endpoint, SOURCE_STALE_CACHE)
                    if stale is not None:
                        self._memory = stale
                        logger.json(
                            "WARNING",
                            "Live introspection failed; serving last known-good cached schema",
                            {**stale.describe(), "error": str(live_error)},
                        )
                        return stale
                raise

            self._memory = loaded
            logger.json("INFO", "Schema refreshed from live introspection", loaded.describe())
            return loaded

    def _memory_hit(self, endpoint: str) -> Optional[LoadedSchema]:
        cached = self._memory
        if cached is None or cached.endpoint != endpoint:
            return None
        if not self._is_fresh(cached.refreshed_at):
            return None
        # Same GraphQLSchema object, relabelled -- no rebuild, no disk, no network.
        return dataclasses.replace(cached, source=SOURCE_MEMORY)

    def invalidate(self) -> None:
        """Drop the in-memory schema. Leaves the disk cache alone."""
        self._memory = None

    @property
    def endpoint(self) -> str:
        """The configured endpoint, as used for cache identity."""
        return self._endpoint()

    def current(self) -> Optional[LoadedSchema]:
        """The in-memory schema, if any. Never triggers a load."""
        return self._memory


# Process-level default instance. The MCP server is a long-lived process with a
# module-level agent singleton, so this is the natural home for the cache.
schema_cache = SchemaCache()


async def get_schema(force: bool = False) -> GraphQLSchema:
    """Convenience accessor returning just the schema object."""
    loaded = await schema_cache.get_schema(force=force)
    return loaded.schema
