"""memrack — memory provider with a pluggable backend rack.

One provider slot; the rack hosts many backends. v0.2 semantics (SPEC):
the AGENT routes — every backend advertises a card (best_for / limits /
search); `memrack_write` takes the agent's backend choice, `memrack_search`
fans out and merges. Fallback rules are deterministic by SOURCE (tool, not
content); no LLM judge anywhere — not at write ("Dreaming" lesson,
2026-08-29) and not at read (no 'auto'-mode).

The Basisfall is design law: with only the built-in `base-db` backend
(stdlib-only), the whole memory works — fresh HERMES_HOME, zero docking
(design law — the proof lives as the T-013 test on the board; the runtime half lands with T-008).
"""

import importlib.util
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

_LOG = logging.getLogger("memrack")

PLUGIN_NAME = "memrack"
__version__ = "0.4.0"

# Write kinds (plain strings: docked modules load via spec_from_file_location
# and compare by VALUE, never by identity).
# Write-KINDS vocabulary (third-man A-3 adopted: semantic is a SEARCH MODE (vec),
# curation happens via file tools/curation imports — neither is a write kind).
EXPLICIT = "explicit"   # scalar facts, config values, exact identifiers
CURATED = "curated"     # facts imported from curation (memrack import / kuratur)
EPISODIC = "episodic"   # routing-only kind: turns live in Hermes state.db, never copied
WRITE_KINDS = (EXPLICIT, CURATED)   # what memrack_write accepts

DEFAULT_BACKEND = "base-db"   # the stdlib floor (SPEC §1.4) — solo-functional
_DEFAULT_ENABLED = ("base-db",)   # plugins.memrack.enabled_backends default; wiki = opt-in migration bridge


_SEARCH_MODES = ("fts", "vec", "pointers")


_MEMORY_WRITE_TYPES = tuple(
    (action, EXPLICIT) for action in ("add", "patch", "replace", "remove"))


def _utcnow_iso():
    """UTC, Mikrosekunden, Z-Suffix — String-Vergleich über Backends hinweg konsistent (A-5)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
#
# ── backend registry ─────────────────────────────────────────────────────────

_BACKEND_METHODS = (
    "initialize", "on_turn", "on_memory_write", "on_session_end",
    "prefetch", "system_prompt_block", "shutdown", "export",
)
_BACKEND_OPTIONAL = ("search", "write", "touch", "tool_schemas", "handle_tool_call")


def ensure_backend(obj, origin: str):
    """Validate the docking contract; a miss raises TypeError (registry skips it)."""
    missing = [m for m in _BACKEND_METHODS if not callable(getattr(obj, m, None))]
    if missing:
        raise TypeError("backend %r misses: %s" % (origin, ", ".join(missing)))
    if not getattr(obj, "name", None):
        raise TypeError("backend %r misses a name" % origin)
    # card-vs-code: a claimed search mode without a search implementation is a LIE
    modes = getattr(obj, "search_modes", None) or getattr(obj, "search", ())
    if modes and not callable(getattr(obj, "search", None)):
        raise TypeError("backend %r claims search=%r without a search method" % (origin, modes))
    return obj


def _hermes_home(hermes_home=None) -> Path:
    if hermes_home:
        return Path(hermes_home)
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def docked_dir(hermes_home=None) -> Path:
    """User-docking directory: $HERMES_HOME/plugins/memrack/backends.d/."""
    return _hermes_home(hermes_home) / "plugins" / PLUGIN_NAME / "backends.d"


def _load_backend_file(path: Path):
    """Load one backend module; contract: the module exposes export_backend()."""
    spec = importlib.util.spec_from_file_location(
        "memrack_backend_%s" % path.stem.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise ImportError("no loader for %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return ensure_backend(mod.export_backend(), origin=path.name)


def _load_manifest(path: Path):
    """Bridge-pattern docking: <name>.yaml IS the card (presence = registered).
    Fields: name, module (required); best_for/limits/search/accepts (optional)."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("docked manifests need pyyaml: %s" % exc) from exc
    with open(path, encoding="utf-8") as fh:
        man = yaml.safe_load(fh) or {}
    if not man.get("name"):
        raise TypeError("manifest %s misses a name" % path.name)
    module = man.get("module")
    if not module:
        raise TypeError("manifest %s misses its module field" % path.name)
    module_path = Path(module)
    if not module_path.is_absolute():
        module_path = path.parent / module
    if not module_path.exists():
        raise FileNotFoundError("manifest %s points to missing module %s" % (path.name, module_path))
    backend = _load_backend_file(module_path)
    if backend.name != man["name"]:
        raise TypeError("manifest %r names backend %r but code says %r"
                        % (path.name, man["name"], backend.name))
    backend.manifest = man  # the card is DATA (SPEC §1.3) — attached, verified against code
    modes = man.get("search") or ()
    if modes and not callable(getattr(backend, "search", None)):
        raise TypeError("manifest %s claims search=%r but the code ships no search method"
                        % (path.name, modes))   # a card that lies is worse than no card
    return backend


def _card(backend) -> dict:
    """The card the agent sees: manifest data wins over code attrs (both verified at load)."""
    man = getattr(backend, "manifest", None) or {}
    dyn = getattr(backend, "card_modes", None)
    live = tuple(dyn()) if callable(dyn) else None
    declared = tuple(man.get("search") or ())
    # Beweis-Lektion (SDK-Nachweis #4): die Karte verspricht NIE mehr als die LIEFERUNG —
    # der Manifest kann sie NICHT über die ehrliche (Umgebungs-)Karte hinaus überzeugen.
    modes = (live if live is not None
             else (declared or tuple(getattr(backend, "search_modes", ()))) )
    best_for = man.get("best_for") or getattr(backend, "best_for", "")
    lim = man.get("limits") or getattr(backend, "limits", "")
    return {
        "name": backend.name,
        "best_for": best_for,
        "limits": lim,
        "modes": list(modes or ()),
        "write": callable(getattr(backend, "write", None)),
        "search": callable(getattr(backend, "search", None)),
        "built_in": getattr(backend, "built_in", False),
        "origin": (man.get("module") or "code") if man else "code",
        "declared": sorted(set(declared) - set(modes)),   # Manifest-Promisse OHNE Auslieferung (env)
    }


def discover_backends(hermes_home=None, enabled_builtins=None) -> dict:
    """Built-ins live in backends/ beside this file (code-first, plugin-shipped);
    users dock via backends.d/ manifest-first (bridge pattern: presence = registered).

    enabled_builtins: the config gate for code-first built-ins. None = all.
    A failing module/manifest is skipped and reported — one bad dock never kills the rack."""
    result = {"backends": {}, "errors": [], "parked": [], "gaps": []}
    dock = docked_dir(hermes_home)
    candidates = sorted((Path(__file__).resolve().parent / "backends").glob("*.py"))
    if dock.is_dir():
        candidates += sorted(list(dock.glob("*.yaml")) + list(dock.glob("*.yml")))
        candidates += [p for p in sorted(dock.glob("*.py"))]  # legacy code-first drop-ins still work
    # pass 0: module files REFERENCED by manifests are docked via their manifest —
    # never ALSO load them as legacy code-first drop-ins (double-registration trap)
    referenced = set()
    if dock.is_dir():
        for mpath in list(dock.glob("*.yaml")) + list(dock.glob("*.yml")):
            try:
                import yaml as _yaml
                man = _yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
                mod = Path(str(man.get("module") or ""))
                referenced.add((mpath.parent / mod if not mod.is_absolute() else mod).resolve())
            except Exception:
                pass   # unparseable manifests surface as registry errors in the main pass
    for path in candidates:
        if path.name.startswith("_") or path.name.startswith("disabled-"):
            continue  # disabled-<name>.yaml = parked without deletion (bridge semantics)
        try:
            if path.suffix in (".yaml", ".yml"):
                backend = _load_manifest(path)
            else:
                if path.resolve() in referenced:
                    continue   # belongs to a manifest — its backend loads THERE
                backend = _load_backend_file(path)
                if path.parent == dock:
                    backend.built_in = False
            if backend.name in result["backends"]:
                result["errors"].append({"file": path.name,
                                         "error": "duplicate backend name %r" % backend.name})
                continue
            if path.parent != dock and enabled_builtins is not None \
                    and backend.name not in tuple(enabled_builtins):
                result["parked"].append({"name": backend.name, "reason": "not in enabled_backends"})
                continue
            result["backends"][backend.name] = backend
        except Exception as exc:
            result["errors"].append({"file": path.name,
                                     "error": "%s: %s" % (type(exc).__name__, exc)})
    return result


# ── provider core (ABC binding happens in register()) ────────────────────────

class _MemrackBase:
    """Hooks delegate by SOURCE rules; the rack card lives in the prompt block."""

    @property
    def name(self) -> str:
        return PLUGIN_NAME

    # ABC-PFLICHT (fehlte der Skill-ABC-Referenz!): Konfiguration/Deps-Gate, KEINE Netz-Calls.
    # Die Basis-Basis (stdlib, SQLite) braucht nichts — immer bereit.
    def is_available(self) -> bool:
        return True

    # T-014-Substanz: der Setup-Flow (`hermes memory setup memrack`) liest und schreibt diese
    def get_config_schema(self):
        return [
            {
                "key": "backend",
                "description": "Force ONE backend for ALL routes (migration/transition override)",
                "default": "",
                "choices": ["", DEFAULT_BACKEND, "wiki"],
            },
            {
                "key": "enabled_backends",
                "description": "Which built-ins are in the rack (default: base-db only; wiki = opt-in migration bridge)",
                "default": list(_DEFAULT_ENABLED),
                "choices": [list(_DEFAULT_ENABLED), ["base-db", "wiki"]],
            },
            {
                "key": "embedding_model",
                "description": "Ollama embedding model for the optional vec engine",
                "default": "nomic-embed-text",
            },
            {
                "key": "ollama_host",
                "description": "Ollama endpoint for vec embeddings",
                "default": "http://127.0.0.1:11434",
            },
            {
                "key": "search_top_k",
                "description": "Default result count for memrack_search (max 32)",
                "default": 8,
            },
        ]

    def __init__(self, config=None):
        self._config = dict(config or {})
        self._session_id = ""
        self._hermes_home = None
        self._rack: dict = {}
        self._cards: dict = {}
        self._parked: list = []
        self._load_errors: list = []
        self._gaps: list = []
        self._active = None
        self._force = False
        self._route_log = None
        self._lock = threading.Lock()

    # ── lifecycle ──
    def initialize(self, session_id="", **kwargs):
        self._session_id = session_id or ""
        self._hermes_home = str(_hermes_home(kwargs.get("hermes_home")))
        kwargs.pop("hermes_home", None)  # we own this argument; no duplicate kwarg to backends

        enabled = self._config.get("enabled_backends", _DEFAULT_ENABLED)
        rack = discover_backends(self._hermes_home, enabled_builtins=enabled)
        self._rack = rack["backends"]
        self._parked = rack["parked"]
        self._load_errors = rack["errors"]
        self._cards = {n: _card(b) for n, b in self._rack.items()}

        # initialize every rack member; a member that fails to init = Gap (Degradations-Leiter),
        # but the DEFAULT/FORCED target failing is fatal (Basisfall-Pflicht).
        target = self._pick_default_target()
        initialized = {}
        for bname, backend in sorted(self._rack.items()):
            try:
                backend.initialize(
                    session_id=self._session_id, hermes_home=self._hermes_home,
                    provider_config=self._config, **kwargs)
                initialized[bname] = backend
            except Exception as exc:
                self._gaps.append({"backend": bname,
                                   "error": "%s: %s" % (type(exc).__name__, exc)})
                if bname == target:
                    raise RuntimeError(
                        "memrack: default backend %r failed to initialize: %s"
                        % (target, exc)) from exc
        self._rack = initialized

        log_dir = Path(self._hermes_home) / "plugin-data" / PLUGIN_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(log_dir, 0o700)
        except OSError:
            pass
        self._route_log = log_dir / "route_log.jsonl"
        self._active = self._rack.get(target) or (self._rack.get(DEFAULT_BACKEND))
        forced = self._config.get("backend")
        if forced and forced in self._rack:
            self._force = True
        _LOG.info("memrack ready (v%s): backends=%s active=%s gaps=%s parked=%s errors=%s",
                  __version__, sorted(self._rack),
                  self._active.name if self._active else None,
                  self._gaps or "none", self._parked or "none",
                  self._load_errors or "none")

    def _pick_default_target(self):
        forced = self._config.get("backend")
        target = forced or DEFAULT_BACKEND
        if target not in self._rack and len(self._rack) == 1:
            # Basisfall-Regel: single-backend-rack → dorthin, egal wie es heißt
            target = next(iter(self._rack))
        return target

    # ── routing bookkeeping ──
    def _routed_for_kind(self, kind: str):
        """Source-rule routing (deterministic, no content judgement):
        forced > rules: episodic/turn → wiki (session-writer) if enabled else None;
        explicit/semantic/curation → the default floor backend."""
        if self._force:
            return self._active
        if kind == EPISODIC:
            return self._rack.get("wiki")   # session-writer, oder None (state.db ist kanonisch)
        return self._rack.get(self._pick_default_target_for_writes())   # explicit/curated

    def _pick_default_target_for_writes(self):
        forced = self._config.get("backend")
        if forced and forced in self._rack:
            return forced
        return DEFAULT_BACKEND if DEFAULT_BACKEND in self._rack else self._pick_default_target()

    def _log_route(self, kind: str, detail: str, backend: str):
        if not self._route_log:
            return
        entry = json.dumps({
            "ts": _utcnow_iso(),
            "kind": kind, "detail": detail, "backend": backend,
            "session_id": self._session_id,
        }, ensure_ascii=False)
        with self._lock:
            try:
                with open(self._route_log, "a", encoding="utf-8") as fh:
                    fh.write(entry + "\n")
            except OSError as exc:  # read-only home — never break a turn over the log
                _LOG.warning("memrack route log failed: %s", exc)

    # ── provider hooks (Source-rule delegation) ──
    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        if not (user_content or assistant_content):
            return None
        backend = self._routed_for_kind(EPISODIC)
        if backend is None:
            # Turns live in Hermes' state.db; memrack only records the routing fact.
            self._log_route("turn", "unstored (state.db is canonical)", "—")
            return None
        out = backend.on_turn(user_content, assistant_content,
                              session_id=session_id or self._session_id)
        self._log_route("turn", "episodic", backend.name)
        return out

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        backend = self._routed_for_kind(EXPLICIT)
        if backend is None:
            return {"status": "unrouted", "reason": "no explicit-facts backend in rack"}
        out = backend.on_memory_write(action, target, content, metadata=metadata,
                                      session_id=session_id or self._session_id)
        self._log_route("memory_write:%s" % action, str(target), backend.name)
        return out

    def on_session_end(self, messages=None, **kwargs):
        if self._active is None:
            return None
        out = self._active.on_session_end(messages, session_id=self._session_id)
        self._log_route("session_end", "delegation to %s" % self._active.name, self._active.name)
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
        parts = []
        if self._active is not None:
            base = self._active.system_prompt_block() or ""
            if base:
                parts.append(base)
        card_lines = self._card_block()
        if card_lines:
            parts.append(card_lines)
        return "\n\n".join(parts)

    def _card_block(self) -> str:
        """The rack card (SPEC §1.1): one line per backend, tiny, informs the agent's choices."""
        if not self._rack:
            return ""
        lines = ["memrack rack (v%s): wähle Ziele bewusst bei memrack_write;" % __version__]
        for bname in sorted(self._cards):
            c = self._cards[bname]
            extra = ""
            if c.get("declared"):
                extra = " | deklariert-ohne-Auslieferung: %s" % ",".join(c["declared"])
            lines.append("- %s: %s | search: %s%s%s%s" % (
                bname, c["best_for"] or "",
                ",".join(c["modes"]) or "none",
                "" if c["write"] else " | kein atomarer Write",
                " (built-in)" if c["built_in"] else "",
                extra))
        if len(lines) > 9:
            overflow = len(self._cards) - 8
            lines = lines[:9] + ["- … +%d weitere Backends — memrack_status" % overflow]
        return "\n".join(lines)

    def shutdown(self):
        for backend in list(self._rack.values()):
            try:
                backend.shutdown()
            except Exception as exc:
                _LOG.warning("memrack backend %s shutdown failed: %s",
                             getattr(backend, "name", "?"), exc)

    def backup_paths(self):
        paths = [str(self._route_log)] if self._route_log else []
        for backend in self._rack.values():
            path = getattr(backend, "db_path", None)
            if path:
                paths.append(str(path))
        return paths

    # ── tools: search / write / status (SPEC §3, §1.2) ──
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
        our = {
            "memrack_search": {
                "type": "object", "properties": {
                    "query": {"type": "string"},
                    "mode": {"type": "string",
                             "enum": list(_SEARCH_MODES) + ["all"],
                             "default": "all"},
                    "backend": {"type": "string",
                                "default": None},
                    "limit": {"type": "integer", "default": 8},
                }, "required": ["query"],
                "additionalProperties": False,
            },
            "memrack_write": {
                "type": "object", "properties": {
                    "content": {"type": "string"},
                    "backend": {"type": "string", "default": None},
                    "kind": {"type": "string", "enum": list(WRITE_KINDS), "default": EXPLICIT},
                    "tags": {"type": "array", "items": {"type": "string"}},
                }, "required": ["content"],
                "additionalProperties": False,
            },
            "memrack_status": {"type": "object", "properties": {},
                               "required": [], "additionalProperties": False},
            "memrack_import": {
                "type": "object", "properties": {
                    "source": {"type": "string",
                               "enum": ["memory", "user", "both"], "default": "both"},
                }, "required": [], "additionalProperties": False,
            },
        }
        for name, params in our.items():
            if name not in seen:
                schemas.append({"type": "function",
                                "function": {"name": name,
                                             "description": _TOOL_DESCRIPTIONS[name],
                                             "parameters": params}})
        return schemas

    def handle_tool_call(self, tool_name, args, **kwargs):
        args = dict(args or {})
        if tool_name == "memrack_search":
            return self._tool_search(args)
        if tool_name == "memrack_write":
            return self._tool_write(args)
        if tool_name == "memrack_import":
            return self._tool_import(args)
        if tool_name == self._status_tool_name():
            return json.dumps(self.status(), indent=2, ensure_ascii=False)
        if self._active is not None:
            fn = getattr(self._active, "handle_tool_call", None)
            if fn is not None:
                return fn(tool_name, args, **kwargs)
        raise KeyError("unknown memrack tool %r" % tool_name)

    # ── search: fan-out + EINE Fusionsregel (SPEC §3.2/§3.3) ──
    def _tool_search(self, args):
        started = time.time()
        query = str(args.get("query") or "").strip()
        mode = args.get("mode") or "all"
        backend_wish = args.get("backend") or None
        limit = max(1, min(int(args.get("limit") or self._config.get("search_top_k") or 8), 32))
        if not query:
            return json.dumps({"error": "query required",
                               "hint": "Was suchst du? Ein Stichwort reicht, z.B. 'Relay Port'"},
                              ensure_ascii=False)

        chosen = list(self._rack.values())
        if backend_wish:
            if backend_wish not in self._rack:
                return json.dumps({
                    "error": "unknown backend %r" % backend_wish,
                    "hint": "Registriert: %s" % ", ".join(sorted(self._rack)),
                }, ensure_ascii=False)
            chosen = [self._rack[backend_wish]]

        requested = list(_SEARCH_MODES) if mode == "all" else [mode]
        if mode != "all" and mode not in _SEARCH_MODES:
            return json.dumps({"error": "unknown mode %r" % mode,
                               "hint": "modes: all|fts|vec|pointers"}, ensure_ascii=False)

        runs, engine_errors = [], []
        carried = sorted({m for b in chosen for m in self._cards[b.name]["modes"]})
        if mode != "all" and mode not in carried:
            # SPEC §1.4 Degradation: ein spezifisch angefragt modus, den KEIN im Rack liegender
            # Backend trägt, ist ein ehrlicher Error — niemals stilles 0-Ergebnis.
            return json.dumps({
                "error": "kein backend trägt mode %r" % mode,
                "hint": "verfügbare Modi: %s" % (", ".join(carried) or "keine"),
            }, ensure_ascii=False)
        for backend in chosen:
            modes = [m for m in requested if m in tuple(self._cards[backend.name]["modes"])]
            if not modes:
                if backend_wish:
                    return json.dumps({
                        "error": "backend %r trägt mode %r nicht" % (backend.name, mode),
                        "hint": "verfügbare Modi: %s" % ", ".join(
                            self._cards[backend.name]["modes"] or ["keine"]),
                    }, ensure_ascii=False)
                continue
            for m in modes:
                try:
                    hits = backend.search(query, m, limit) or []
                    for h in hits:
                        h["mode"] = m
                    runs.append(hits)
                except Exception as exc:
                    engine_errors.append({"backend": backend.name, "mode": m,
                                          "error": "%s: %s" % (type(exc).__name__, exc)})

        merged = self._rrf_merge(runs, limit)
        ms = int((time.time() - started) * 1000)
        self._touch_facts(merged)
        result = {"query": query, "mode": mode, "backend": backend_wish,
                  "ms": ms, "results": merged, "engine_errors": engine_errors}
        if not merged and not engine_errors:
            hint = "0 Treffer."
            if backend_wish:
                hint += " Nur %r durchsucht — probier backend=None für den ganzen Rack." % backend_wish
            hint += " Versuch mode 'vec' (Paraphrasen) oder 'pointers' (Verzeichnis);"
            hint += " breiter formulieren hilft auch."
            result["hint"] = hint
        return json.dumps(result, indent=2, ensure_ascii=False)

    @staticmethod
    def _rrf_merge(runs, limit):
        """RRF 60 + collapse per target + deterministischer Tiebreak — SPEC §3.3."""
        pool: dict = {}
        for hits in runs:
            for hit in hits:
                key = hit.get("target") or "content:%s" % (hit.get("content") or "")
                entry = pool.setdefault(key, {
                    "backend": hit.get("backend"), "kind": hit.get("kind"),
                    "content": hit.get("content"), "target": hit.get("target"),
                    "ts": hit.get("ts"), "hits": hit.get("hits") or 0,
                    "score": 0.0, "found_by": []})
                entry["score"] += 1.0 / (60.0 + max(1, int(hit.get("rank") or 99)))
                entry["found_by"].append(hit.get("mode"))
        merged = list(pool.values())
        merged.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)   # Phase 1: ts DESC
        merged.sort(key=lambda e: (-e["score"], -(e.get("hits") or 0)))    # Phase 2 (stable): score, heat DESC
        merged = merged[:limit]
        for e in merged:
            e["found_by"] = sorted({m for m in e["found_by"] if m})
        return merged

    def _touch_facts(self, merged):
        """hits++ on returned fact rows, grouped per backend (Heat = report basis)."""
        by_backend: dict = {}
        for hit in merged:
            if hit.get("kind") != "fact":
                continue
            tid = str(hit.get("target") or "").rsplit(":", 1)[-1]
            try:
                by_backend.setdefault(hit.get("backend"), []).append(int(tid))
            except ValueError:
                continue
        for bname, ids in by_backend.items():
            engine = self._rack.get(bname)
            if engine is not None and callable(getattr(engine, "touch", None)):
                try:
                    engine.touch(ids)
                except Exception as exc:
                    _LOG.warning("memrack touch failed (%s): %s", bname, exc)


    # ── T-020: Alt-Import-Job (third-man B-2) — VORBEDINGUNG der Profil-Migration ──
    def _tool_import(self, args):
        """Liest builtin-memory-Files (MEMORY.md/USER.md, §-delimited) und importiert
        jeden Eintrag als fact-Row: hash-dedupe = idempotent (doppelt laufen lassen
        schadet nicht), target='builtin:<file>'. EIN Job pro Profil, kein Cron."""
        source = (args.get("source") or "both")
        if source not in ("memory", "user", "both"):
            return json.dumps({"error": "source: memory|user|both"}, ensure_ascii=False)
        mem_dir = Path(self._hermes_home) / "memories"
        files = (
            [("MEMORY.md", "builtin:MEMORY.md")] if source in ("memory", "both") else []
        ) + ([("USER.md", "builtin:USER.md")] if source in ("user", "both") else [])
        backend = self._routed_for_kind(EXPLICIT)
        if backend is None:
            return json.dumps({"error": "kein explicit-floor Backend im Rack"}, ensure_ascii=False)
        if not callable(getattr(backend, "write", None)):
            return json.dumps({"error": "Backend %r trägt keinen write" % backend.name},
                              ensure_ascii=False)
        report = {"imported": 0, "duplicates": 0, "files": [], "errors": []}
        for fname, prefix in files:
            path = mem_dir / fname
            if not path.exists():
                report["files"].append({"file": fname, "entries": 0, "imported": 0,
                                        "duplicates": 0, "note": "not present"})
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            entries = [e.strip() for e in raw.split("\n\u00a7\n") if len(e.strip()) >= 10]
            file_imported = file_dup = 0
            for entry in entries:
                try:
                    # S-3-HARDENING: das Target identifiziert DEN EINTRAG, NICHT die Datei —
                    # der RRF-Merge collapsed per target: ein File-Full-Target würde
                    # N-1 Treffer stillschweigend verschluckt.
                    tag_hash = hashlib.sha256(entry.encode("utf-8")).hexdigest()[:8]
                    out = backend.write(EXPLICIT, entry,
                                        target_hint="%s#%s" % (prefix, tag_hash),
                                        tags=["import:" + fname])
                    if out.get("status") == "stored":
                        report["imported"] += 1
                        file_imported += 1
                    else:
                        report["duplicates"] += 1
                        file_dup += 1
                except Exception as exc:
                    report["errors"].append({"file": fname, "error": str(exc)[:80]})
            report["files"].append({"file": fname, "entries": len(entries),
                                    "imported": file_imported, "duplicates": file_dup})
        self._log_route("import", source, backend.name)
        report["backend"] = backend.name
        return json.dumps(report, indent=2, ensure_ascii=False)

    # ── write: agent chooses the backend (SPEC §1.1/§1.2) ──
    def _tool_write(self, args):
        content = str(args.get("content") or "").strip()
        kind = args.get("kind") or EXPLICIT
        backend_wish = args.get("backend") or None
        tags = args.get("tags") if isinstance(args.get("tags"), list) else None
        if not content:
            return json.dumps({"error": "content required"})
        if kind not in WRITE_KINDS:
            return json.dumps({"error": "kind %r ist kein Write-kind" % kind,
                               "hint": "kinds: explicit|curated — episodic ist Hermes-nativ,"
                                       " semantic ist ein SUCHMODE (vec), kein Speicherziel."},
                              ensure_ascii=False)
        if backend_wish and backend_wish not in self._rack:
            return json.dumps({"error": "unknown backend %r" % backend_wish,
                               "hint": "Registriert: %s" % ", ".join(sorted(self._rack))},
                              ensure_ascii=False)
        backend = self._rack.get(backend_wish) if backend_wish else \
            self._routed_for_kind(kind)   # keine Wahl = deterministische Quelle-Regel
        if backend is None:
            return json.dumps({"error": "kein Backend für kind %r im Rack" % kind,
                               "hint": "memrack_status zeigt die Karten"})
        write_fn = getattr(backend, "write", None)
        if not callable(write_fn):
            return json.dumps({
                "error": "backend %r trägt keinen atomaren Write" % backend.name,
                "hint": "Strukturwissen gehört als Wiki-Seite (Datei-Tools); "
                        "atomare Fakten: memrack_write ohne backend-Param.",
            }, ensure_ascii=False)
        try:
            result = write_fn(kind, content, tags=tags, session_id=self._session_id)
        except Exception as exc:
            return json.dumps({"error": "write failed: %s: %s" % (type(exc).__name__, exc)},
                              ensure_ascii=False)
        self._log_route("memrack_write", kind, backend.name)
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _rescan_dock(self) -> list:
        """Cheap fs-only rescan: manifests on disk that this session's rack has not loaded —
        they live 'registered (next session)' (bridge-watch spirit, SPEC §1.3)."""
        dock = docked_dir(self._hermes_home)
        if not (dock and dock.is_dir()):
            return []
        known = set(self._rack) | {p.get("name") for p in self._parked} | {
            e.get("name") for e in self._load_errors if e.get("name")}
        pending = []
        for path in sorted(list(dock.glob("*.yaml")) + list(dock.glob("*.yml"))):
            if path.name.startswith("_") or path.name.startswith("disabled-"):
                continue
            if path.stem not in known:
                pending.append({"manifest": path.name, "note": "registered (next session)"})
        return pending

    # ── inventory ──
    def status(self) -> dict:
        return {
            "plugin": PLUGIN_NAME,
            "version": __version__,
            "cards": self._cards,
            "backends": sorted(self._rack),
            "gap_list": self._gaps,
            "parked": self._parked,
            "docking_errors": self._load_errors,
            "active_backend": self._active.name if self._active else None,
            "forced_backend": self._config.get("backend"),
            "enabled_builtins": self._config.get("enabled_backends", list(_DEFAULT_ENABLED)),
            "pending_docks": self._rescan_dock(),
            "session_id": self._session_id,
        }


_TOOL_DESCRIPTIONS = {
    "memrack_search": "Suche im memory-rack: hybrid fan-out über alle Backends und Suchmodi "
                      "(default mode=all), oder gezielt ein Backend/Modus. Liefert Treffer mit "
                      "target-Referenzen (wiki-Pfad/L42 etc.) zum ganzseitigen Weiterlesen.",
    "memrack_write": "Speichert ein atomares Fact (ein Satz) im memory-rack; der Agent wählt "
                     "die Ziel-Backend anhand der Rack-Karte (default: base-db). Strukturwissen "
                     "gehört in Wiki-Seiten (Datei-Tools), nicht hierher.",
    "memrack_status": "Rack-Inventar: Backends mit Card (best_for/limits/modes), Heat-Zähler, "
                      "Gaps (ausgefallene Engines), geparkte/dockings-Fehler.",
    "memrack_import": "Importiert Alt-Bestand des builtin-Memory (MEMORY.md/USER.md, §-delimited) "
                      "als fact-Rows — idempotent via hash-dedupe. Migrations-Job, einmal pro Profil.",
}


def register(ctx):
    """Plugin loader entry point: bind a MemoryProvider ABC subclass."""
    try:
        from agent.memory_provider import MemoryProvider
    except ImportError:  # keeps the module importable for tests outside Hermes
        class MemoryProvider:
            pass

    class _BoundMemrackProvider(_MemrackBase, MemoryProvider):
        pass

    ctx.register_memory_provider(_BoundMemrackProvider())
