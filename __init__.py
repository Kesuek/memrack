"""memrack — memory provider with a pluggable backend rack.

One provider slot: a rule-based router sends each write by data TYPE into
the fitting dockable backend; retrieval fans out across the rack and merges
(hybrid retrieval by architecture, not by bolt-on).

Design guardrail: routing is decided by static type rules, never by an LLM
relevance judgement at write time ("Dreaming" lesson, removed 2026-08-29).
Everything is stored; judgement happens at read/curation time.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

_LOG = logging.getLogger("memrack")

PLUGIN_NAME = "memrack"
__version__ = "0.1.0"

# Write types (plain strings: docked backend modules are loaded via
# spec_from_file_location and must compare by VALUE, never identity).
EXPLICIT = "explicit"   # scalar facts, config values, exact identifiers
EPISODIC = "episodic"   # raw conversation turns / session activity
SEMANTIC = "semantic"   # paraphrase-sensitive knowledge snippets
CURATION = "curation"   # curated structured destinations (wiki pages)
ALL_TYPES = (EXPLICIT, EPISODIC, SEMANTIC, CURATION)

DEFAULT_BACKEND = "wiki"  # v0.1 route-all target (wiki-memory wrapper)

# v0.1: route-all — byte-equal to running wiki-memory directly.
# v0.2 turns these rows into per-type engines (fts/vec/...).
ROUTE_TABLE = {
    EXPLICIT: DEFAULT_BACKEND,
    EPISODIC: DEFAULT_BACKEND,
    SEMANTIC: DEFAULT_BACKEND,
    CURATION: DEFAULT_BACKEND,
}

_MEMORY_WRITE_TYPES = (
    ("add", EXPLICIT), ("patch", EXPLICIT),
    ("replace", EXPLICIT), ("remove", EXPLICIT),
)


def route(write_type: str) -> str:
    """Map a write type to its backend name (static, testable, logged)."""
    backend = ROUTE_TABLE.get(write_type)
    if backend is None:
        _LOG.warning("memrack router: unknown write type %r -> %s", write_type, DEFAULT_BACKEND)
        return DEFAULT_BACKEND
    return backend


def pick_backend(backends, write_type: str):
    """Choose a backend by write_types membership (v0.2: per-type scores)."""
    for backend in backends:
        if write_type in tuple(getattr(backend, "write_types", ())):
            return backend
    return None


# ── backend registry ─────────────────────────────────────────────────────────

_BACKEND_METHODS = (
    "initialize", "on_turn", "on_memory_write", "on_session_end",
    "prefetch", "system_prompt_block", "shutdown", "export",
)


def ensure_backend(obj, origin: str):
    """Validate the docking contract; a miss raises TypeError (registry skips it)."""
    missing = [m for m in _BACKEND_METHODS if not callable(getattr(obj, m, None))]
    if missing:
        raise TypeError("backend %r misses: %s" % (origin, ", ".join(missing)))
    if not getattr(obj, "name", None):
        raise TypeError("backend %r misses a name" % origin)
    return obj


def _hermes_home(hermes_home=None) -> Path:
    if hermes_home:
        return Path(hermes_home)
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def docked_dir(hermes_home=None) -> Path:
    """User-docking directory: $HERMES_HOME/plugins/memrack/backends.d/."""
    return _hermes_home(hermes_home) / "plugins" / PLUGIN_NAME / "backends.d"


def _load_backend_file(path: Path):
    """Load one docked backend module; contract: the module exposes export_backend()."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "memrack_backend_%s" % path.stem.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise ImportError("no loader for %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return ensure_backend(mod.export_backend(), origin=path.name)


def discover_backends(hermes_home=None) -> dict:
    """Built-ins live in backends/ beside this file; users dock via backends.d/.
    A failing module is skipped and reported — one bad dock never kills the rack."""
    rack: dict = {}
    errors: list = []
    dock = docked_dir(hermes_home)
    candidates = list((Path(__file__).resolve().parent / "backends").glob("*.py"))
    if dock.is_dir():
        candidates += list(dock.glob("*.py"))
    for path in sorted(candidates):
        if path.name.startswith("_"):
            continue
        try:
            backend = _load_backend_file(path)
            if backend.name in rack:
                errors.append({"file": path.name,
                               "error": "duplicate backend name %r" % backend.name})
                continue
            rack[backend.name] = backend
        except Exception as exc:
            errors.append({"file": path.name,
                           "error": "%s: %s" % (type(exc).__name__, exc)})
    return {"backends": rack, "errors": errors}


# ── provider core (ABC binding happens in register()) ────────────────────────

class _MemrackBase:
    """All hooks delegate to the routed backend; route decisions are logged."""

    @property
    def name(self) -> str:
        return PLUGIN_NAME

    def __init__(self, config=None):
        self._config = dict(config or {})
        self._session_id = ""
        self._hermes_home = None
        self._rack: dict = {}
        self._load_errors: list = []
        self._active = None
        self._force = False
        self._route_log = None
        self._lock = threading.Lock()

    # ── lifecycle ──
    def initialize(self, session_id="", **kwargs):
        self._session_id = session_id or ""
        self._hermes_home = str(_hermes_home(kwargs.get("hermes_home")))
        kwargs.pop("hermes_home", None)  # we own this argument; no duplicate kwarg to backends
        rack = discover_backends(self._hermes_home)
        self._rack = rack["backends"]
        self._load_errors = rack["errors"]

        # ONE path: forced backend via config, else the route-all default.
        forced = self._config.get("backend")
        target = self._rack.get(forced) if forced else self._rack.get(DEFAULT_BACKEND)
        if target is None:
            raise RuntimeError(
                "memrack: backend %r not in rack %s (docking errors: %s)"
                % (forced or DEFAULT_BACKEND, sorted(self._rack), self._load_errors))
        target.initialize(
            session_id=self._session_id, hermes_home=self._hermes_home, **kwargs)
        self._active = target
        self._force = bool(forced)

        log_dir = Path(self._hermes_home) / "plugin-data" / PLUGIN_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(log_dir, 0o700)
        except OSError:
            pass
        self._route_log = log_dir / "route_log.jsonl"
        _LOG.info("memrack ready (v%s): backends=%s active=%s errors=%s",
                  __version__, sorted(self._rack), self._active.name,
                  self._load_errors or "none")

    # ── routing bookkeeping ──
    def _routed(self, write_type: str):
        if self._force:
            return self._active
        backend = pick_backend(list(self._rack.values()), write_type)
        return backend if backend is not None else self._active

    def _log_route(self, kind: str, write_type: str, backend: str):
        if not self._route_log:
            return
        entry = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "kind": kind, "write_type": write_type, "backend": backend,
            "session_id": self._session_id,
        }, ensure_ascii=False)
        with self._lock:
            try:
                with open(self._route_log, "a", encoding="utf-8") as fh:
                    fh.write(entry + "\n")
            except OSError as exc:  # read-only home — never break a turn over the log
                _LOG.warning("memrack route log failed: %s", exc)

    # ── provider hooks ──
    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        if not (user_content or assistant_content):
            return None
        backend = self._routed(EPISODIC)
        out = backend.on_turn(user_content, assistant_content,
                              session_id=session_id or self._session_id)
        self._log_route("turn", EPISODIC, backend.name)
        return out

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        write_type = dict(_MEMORY_WRITE_TYPES).get(action, EXPLICIT)
        backend = self._routed(write_type)
        out = backend.on_memory_write(action, target, content, metadata=metadata,
                                      session_id=session_id or self._session_id)
        self._log_route("memory_write:%s" % action, write_type, backend.name)
        return out

    def on_session_end(self, messages=None, **kwargs):
        if self._active is None:
            return None
        out = self._active.on_session_end(messages, session_id=self._session_id)
        self._log_route("session_end", CURATION, self._active.name)
        return out

    def on_session_switch(self, new_session_id=None, **kwargs):
        return self._forward("on_session_switch", new_session_id, **kwargs)

    def on_pre_compress(self, messages=None, **kwargs):
        return self._forward("on_pre_compress", messages, **kwargs)

    def on_delegation(self, task=None, result=None, **kwargs):
        return self._forward("on_delegation", **kwargs)

    def queue_prefetch(self, query, *, session_id=""):
        return self._forward("queue_prefetch", query,
                             session_id=session_id or self._session_id)

    def _forward(self, hook, *args, **kwargs):
        """Optional-hook pass-through: a backend not defining it is a no-op."""
        if self._active is None:
            return None
        fn = getattr(self._active, hook, None)
        if fn is None:
            return None
        return fn(*args, **kwargs)

    def prefetch(self, query, *, session_id=""):
        if self._active is None:
            return ""
        return self._active.prefetch(query, session_id=session_id or self._session_id) or ""

    def system_prompt_block(self) -> str:
        if self._active is None:
            return ""
        return self._active.system_prompt_block() or ""

    def shutdown(self):
        if self._active is not None:
            self._active.shutdown()

    def backup_paths(self):
        return [str(self._route_log)] if self._route_log else []

    # ── tools (additive: routed backend keeps its own tool surface) ──
    @staticmethod
    def _status_tool_name():
        return "memrack_status"

    def get_tool_schemas(self):
        schemas, seen = [], set()
        if self._active is not None:
            prop = getattr(self._active, "tool_schemas", None)
            if callable(prop):
                prop = prop()
            for schema in (prop or []):
                fname = (schema.get("function") or {}).get("name")
                if fname and fname not in seen:
                    seen.add(fname)
                    schemas.append(schema)
        if self._status_tool_name() not in seen:
            schemas.append({
                "type": "function",
                "function": {
                    "name": self._status_tool_name(),
                    "description": "List memrack backends, the route table and recent router decisions.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            })
        return schemas

    def handle_tool_call(self, tool_name, args, **kwargs):
        if self._active is not None and tool_name != self._status_tool_name():
            fn = getattr(self._active, "handle_tool_call", None)
            if fn is not None:
                return fn(tool_name, args, **kwargs)
        if tool_name == self._status_tool_name():
            return json.dumps(self.status(), indent=2, ensure_ascii=False)
        raise KeyError("unknown memrack tool %r" % tool_name)

    # ── inventory ──
    def status(self) -> dict:
        return {
            "plugin": PLUGIN_NAME,
            "version": __version__,
            "backends": {b: self._rack[b].export() for b in sorted(self._rack)},
            "docking_errors": self._load_errors,
            "active_backend": self._active.name if self._active else None,
            "forced_backend": self._config.get("backend"),
            "route_table": dict(ROUTE_TABLE),
            "session_id": self._session_id,
        }


def register(ctx):
    """Plugin loader entry point: bind a MemoryProvider ABC subclass."""
    try:
        from agent.memory_provider import MemoryProvider
    except ImportError:  # keeps the module importable for tests outside Hermes
        class MemoryProvider:
            pass

    class _BoundMemrackProvider(MemoryProvider, _MemrackBase):
        pass

    ctx.register_memory_provider(_BoundMemrackProvider())
