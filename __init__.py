"""memrack — memory provider with a pluggable backend rack.

One provider slot; the rack hosts many backends. v0.2 semantics:
the AGENT routes — every backend advertises a card (best_for / limits /
search); `memrack_write` takes the agent's backend choice, `memrack_search`
fans out and merges. Fallback rules are deterministic by SOURCE (tool, not
content); no LLM judge anywhere — not at write ("Dreaming" lesson,
2026-08-29) and not at read (no 'auto'-mode).

The Basisfall is design law: with only the built-in `base-db` backend
(stdlib-only), the whole memory works — fresh HERMES_HOME, zero docking.
"""

import contextlib
import importlib.util
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

_LOG = logging.getLogger("memrack")

PLUGIN_NAME = "memrack"
__version__ = "0.11.1"

# The initialize side-effect block's cross-process exclusivity: one flock on
# plugin-data/memrack/init.lock with a generous busy budget, polled in small
# steps; every side effect inside the block is idempotent, so a busy timeout
# degrades to an honest skip, never an error.
_SIDE_EFFECT_LOCK_TIMEOUT_S = 10.0
_SIDE_EFFECT_LOCK_POLL_S = 0.05

# Write kinds (plain strings: docked modules load via spec_from_file_location
# and compare by VALUE, never by identity).
# Write-KINDS vocabulary (A-3 adopted: semantic is a SEARCH MODE (vec),
# curation happens via file tools/curation imports — neither is a write kind).
EXPLICIT = "explicit"   # scalar facts, config values, exact identifiers
CURATED = "curated"     # facts imported from curation (memrack import / kuratur)
EPISODIC = "episodic"   # routing-only kind: turns live in Hermes state.db, never copied
WRITE_KINDS = (EXPLICIT, CURATED)   # what memrack_write accepts

DEFAULT_BACKEND = "base-db"   # the stdlib floor (the degradation law) — solo-functional
# state-episodic default-ON (read-only over the state.db — memory queries
# search the session history automatically; the state.db stays canonical/ro —
# no copy, no trigger)
_DEFAULT_ENABLED = ("base-db", "state-episodic")   # plugins.memrack.enabled_backends default; wiki = opt-in migration bridge
# THE SHIPPED code-first built-ins (the backends/ dir members: base_db.py,
# state_epi.py, wiki.py) — the pre-init wizard surface still names every
# shipped family member (wiki is opt-in, so its toggle default renders falsy);
# post-init the rack/parked inventory refines this set.
_SHIPPED_BUILTINS = ("base-db", "state-episodic", "wiki", "vec")


_SEARCH_MODES = ("fts", "vec", "pointers", "episodic")


def _truthy(value):
    """Read-side truth for toggles: wizard writes "true"/"false" strings,
    the desktop writes native bools — both must mean the same."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "yes", "on", "1")


def _norm_backends(value):
    """Wizard-safe read of enabled_backends — the setup wizard writes ONE
    string (comma list), a hand-edited config.yaml writes a yaml list; both must
    rack identically. Unknown names are NOT filtered here: the registry parks
    them (honest inventory), this only repairs the TYPE."""
    if isinstance(value, str):
        items = [t.strip() for t in value.split(",") if t.strip()]
        return items or list(_DEFAULT_ENABLED)
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return list(_DEFAULT_ENABLED)


def _config_surface_module():
    """The T-036 config-surface seam module (memrack/config_surface.py beside
    THIS file), spec-path resolved and cached one-copy per process — the same
    convention the docking machine uses. An already-loaded copy under the
    canonical name wins: plugin hosts and test doubles exec this file
    standalone, and sharing their object keeps one module per process (so
    their attribute patches stay visible, too)."""
    import sys
    cs = sys.modules.get("memrack_config_surface")
    if cs is not None:
        return cs
    target = Path(__file__).resolve().parent / "memrack" / "config_surface.py"
    spec = importlib.util.spec_from_file_location("memrack_config_surface", target)
    if spec is None or spec.loader is None:
        raise ImportError("config surface not resolvable beside the provider core")
    cs = importlib.util.module_from_spec(spec)
    sys.modules["memrack_config_surface"] = cs
    spec.loader.exec_module(cs)
    return cs


def _docking_machine_module():
    """The docking machine module (memrack/docking.py beside THIS file),
    spec-path resolved and cached one-copy per process — the SAME convention
    `_config_surface_module` carries: one machine per process, and the
    generated-shim marker the machine stamps is consulted through IT (one
    marker definition in the repo: the stamper decides what counts)."""
    import sys
    mod = sys.modules.get("memrack_docking_machine")
    if mod is not None:
        return mod
    target = Path(__file__).resolve().parent / "memrack" / "docking.py"
    spec = importlib.util.spec_from_file_location("memrack_docking_machine", target)
    if spec is None or spec.loader is None:
        raise ImportError("docking machine not resolvable beside the provider core")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memrack_docking_machine"] = mod
    spec.loader.exec_module(mod)
    return mod


def _is_machine_shim(path) -> bool:
    """Whether one backends.d code file carries the machine's generated-shim
    provenance header (the remove/orphan law's trust anchor). An unresolvable
    machine reads as not-generated — the legacy contract stands for code
    files whose provenance cannot be checked."""
    try:
        return bool(_docking_machine_module().is_generated_shim(path))
    except Exception:
        return False


_MEMORY_WRITE_TYPES = tuple(
    (action, EXPLICIT) for action in ("add", "patch", "replace", "remove"))


def _write_accepts_target_hint(fn) -> bool:
    """One signature probe for the import job: a foreign backend whose write() ignores
    target_hint degrades to a plain write (log + proceed) instead of a TypeError per entry."""
    try:
        params = inspect.signature(fn).parameters
    except (ValueError, TypeError):
        return False
    for param in params.values():
        if param.kind is param.VAR_KEYWORD or param.name == "target_hint":
            return True
    return False


def _utcnow_iso():
    """UTC, microseconds, Z-suffix — string comparison consistent across backends (A-5)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
#
# ── backend registry ─────────────────────────────────────────────────────────

_BACKEND_METHODS = (
    "initialize", "on_turn", "on_memory_write", "on_session_end",
    "prefetch", "system_prompt_block", "shutdown", "export",
)
_BACKEND_OPTIONAL = ("search", "write", "touch", "tool_schemas", "handle_tool_call")

# A mirrored docked tool never carries the provider's original name across the
# host boundary (the mirror law); the sentinel marks "no mirrored claim" in
# the dispatch lookup.
_MIRROR_MISS = object()


def _schema_name(schema):
    """The tool name of ONE tool-schema dict, in EITHER ABC shape (function-
    wrapped {"type": "function", "function": {"name": ...}} or the flat
    {"name": ...} form); None when the dict carries no name."""
    if not isinstance(schema, dict):
        return None
    function = schema.get("function")
    if isinstance(function, dict):
        return function.get("name")
    return schema.get("name")


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


def _host_yaml_writer(home):
    """`hermes_cli.config.save_config` — the HOST's own config.yaml writer,
    when this process has the host tree importable AND the write targets the
    host context's home (the host writer is home-bound to its calling
    context; an explicit foreign ``hermes_home=`` keeps the in-plugin
    writer, which targets exactly that home). None = unavailable
    (hermes-less): the guarded in-plugin writers run."""
    try:
        from hermes_cli import config as _host_config
        host_path = Path(_host_config.get_config_path()).resolve()
    except Exception:
        return None
    if host_path.parent != Path(home).resolve():
        return None
    return _host_config.save_config


def _host_json_writer():
    """The host's atomic json sidecar writer (``utils.atomic_json_write``
    — the real location in the host tree; ``hermes_cli.utils`` rides along
    as the plan-named alias probe) when importable; None = hermes-less,
    the in-plugin tmp/replace writer runs."""
    for module_name in ("utils", "hermes_cli.utils"):
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        writer = getattr(module, "atomic_json_write", None)
        if callable(writer):
            return writer
    return None


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
    backend.manifest = man  # the card is DATA (the docking law) — attached, verified against code
    backend.manifest_file = path.name   # the rescan keys on this FILE, never the backend name (T-029 bug 3)
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
    # Proof lesson (SDK proof #4): the card NEVER promises more than the DELIVERY —
    # the manifest cannot out-claim the honest (environment-derived) card.
    modes = (live if live is not None
             else (declared or tuple(getattr(backend, "search_modes", ()))) )
    best_for = man.get("best_for") or getattr(backend, "best_for", "")
    lim = man.get("limits") or getattr(backend, "limits", "")
    out = {
        "name": backend.name,
        "best_for": best_for,
        "limits": lim,
        "modes": list(modes or ()),
        "write": callable(getattr(backend, "write", None)),
        "search": callable(getattr(backend, "search", None)),
        "built_in": getattr(backend, "built_in", False),
        "origin": (man.get("module") or "code") if man else "code",
        "declared": sorted(set(declared) - set(modes)),   # manifest promise WITHOUT delivery (env)
    }
    if not out["modes"]:
        # The card reports what IS: a mode-bearing backend whose live probe came up
        # empty carries the probe's honest reason (one copy with the fresh-init cards —
        # the CLI inventory pipeline shows the same reason, never a silent empty modes).
        promised = declared or tuple(getattr(backend, "search_modes", ()))
        gap = getattr(backend, "_vec_gap", None)
        if promised and isinstance(gap, str) and gap:
            out["env_gap"] = gap
    if man:   # catalog-dock additions (sparse, additive only): the mirrored
        # effective tool names (the Namensgesetz duty — they always sit on the
        # status card) and the draft marker until the human review ran.
        mirrors = man.get("mirror_tools")
        if isinstance(mirrors, dict) and mirrors:
            out["mirrored_tools"] = dict(mirrors)
        if _truthy(man.get("forward_turns")):
            # the opt-in turn gate rides the card (sparse, additive only):
            # the status card is the surface the gate's recorded decision
            # stays visible on (a docked provider receiving turn events).
            out["forward_turns"] = True
        if _truthy(man.get("forward_transcript")):
            # the transcript gate's recorded decision rides the card the
            # same way: a dock receives the session-end transcript only
            # when THE card records it (default off).
            out["forward_transcript"] = True
        if man.get("draft"):
            out["draft"] = True
            # T-036 Addendum (3): a card with PROPOSED-Text (draft:true)
            # pends confirmation — the flags ride the card dict on EVERY
            # surface it is served in (status cards-slot + the additive
            # cards slots of memrack_search/memrack_write: _cards_now
            # rebuilds through _card on every call, so every tool answer
            # carries them). A reviewed card carries no flags.
            try:
                out.update(_config_surface_module().pending_flags(man))
            except Exception as exc:
                _LOG.warning("memrack pending flags read failed: %s", exc)
    return out


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
            from_dock = path.parent == dock
            if path.suffix in (".yaml", ".yml"):
                backend = _load_manifest(path)
            else:
                if path.resolve() in referenced:
                    continue   # belongs to a manifest — its backend loads THERE
                # the machine-generated dock shim is bound to its CARD, never a
                # standalone backend: a shim left WITHOUT a referencing card
                # (the remove law's orphan) does not resurrect the provider as
                # a legacy drop-in — honestly reported, never silently skipped
                if _is_machine_shim(path):
                    result["errors"].append({
                        "file": path.name,
                        "error": "orphan generated dock shim ignored — no card "
                                 "references it (the card was removed; the shim "
                                 "does not load as a legacy drop-in)"})
                    continue
                backend = _load_backend_file(path)
            if from_dock:
                # T-036: an honest docked-file marker — the membership
                # family (the config surface's `<name>_in_rack` toggles)
                # distinguishes backends.d members from the shipped
                # built-ins by THIS flag, not by card-shape heuristics
                # (built-ins carry no built_in attr; test_router pins False).
                backend.built_in = False
                backend.docked_file = True
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

    # ABC duty (was missing in the skill ABC reference!): config/deps gate, NO network calls.
    # The base layer (stdlib, SQLite) needs nothing — always ready.
    def is_available(self) -> bool:
        return True

    # read and written by the setup flow (`hermes memory setup memrack`) and
    # the dashboard's provider setup modal (web_server_memory normalizes
    # from THIS method) — instanz-beziehbar since T-036
    def get_config_schema(self):
        """Wizard-shaped schema list. The six static fields stay additive on
        the surface; the T-036 Addendum (1)/(2) family is appended
        instance-derived: one `<name>_in_rack` bool toggle per known backend
        (built-ins = rack members + parked built-ins + the two default names;
        docked = the rack's docked cards) plus the `dock_want` select with the
        machine's current offer list. A failing dynamic derivation degrades
        honestly to the static fields (the setup must never crash — the
        Basisfall keeps its exact shape)."""
        fields = [
            {
                "key": "backend",
                "description": "Force ONE backend for ALL routes (migration/transition override)",
                "default": "",
                "choices": ["", DEFAULT_BACKEND, "wiki"],
            },
            {
                "key": "enabled_backends",
                "description": "Rack membership (comma list): base-db is always included as the "
                               "floor; wiki/state-episodic opt-in",
                "default": ",".join(_DEFAULT_ENABLED),
                "choices": [",".join(c) for c in (
                    ("base-db",), ("base-db", "state-episodic"),
                    ("base-db", "wiki"), ("base-db", "state-episodic", "wiki"))],
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
                "key": "auto_enable",
                "description": "On activation, enable base-db + wiki automatically (merges "
                               "into enabled_backends; default off — nothing enabled "
                               "without an explicit toggle)",
                "default": "false",
                "choices": ["true", "false"],
            },
            {
                "key": "search_top_k",
                "description": "Default result count for memrack_search (max 32)",
                "default": 8,
            },
            {
                "key": "dock_retention_days",
                "description": "Days an uninstalled dock card stays parked before card + shim "
                               "are removed (0 = immediately, -1 = never; backups land in "
                               "plugin-data/memrack/removed-cards)",
                "default": 14,
            },
            {
                "key": "fanout_timeout_s",
                "description": "Per-backend hook fan-out timeout in seconds (default 5): "
                               "a hanging backend becomes an honest gap after this budget, "
                               "the rest of the rack still delivers; 0 or non-numbers "
                               "fall back to the default",
                "default": 5,
            },
            {
                "key": "prefetch_max_chars",
                "description": "Total character budget for the merged prefetch block "
                               "(default 4000): longer merges truncate with an honest "
                               "'+N chars truncated' note; 0 or non-numbers fall back "
                               "to the default",
                "default": 4000,
            },
        ]
        for builder in (self._membership_toggle_fields, self._dock_want_field_now):
            try:
                built = builder()
                fields += list(built) if isinstance(built, list) else [built]
            except Exception as exc:
                _LOG.warning("memrack dynamic config fields (%s) failed: %s",
                             getattr(builder, "__name__", "?"), exc)
        return fields

    def _membership_toggle_fields(self) -> list:
        """The dynamic `<name>_in_rack` toggles (T-036 Addendum (1)) — the
        built-in candidates are rack members + parked built-ins + the two
        default names; the docked candidates are the rack's docked cards
        (presence family). Parked dock cards reappear via the dock_want
        re-dock, not as toggles (presence state, honest defaults)."""
        rack = getattr(self, "_rack", None) or {}
        built = {name for name, backend in rack.items()
                 if getattr(backend, "built_in", False)}
        parked = getattr(self, "_parked", None) or []
        for entry in parked:
            if isinstance(entry, dict) and entry.get("name"):
                built.add(str(entry["name"]))
        built |= (set(_DEFAULT_ENABLED) | {DEFAULT_BACKEND} |
                  set(_SHIPPED_BUILTINS))
        docked = [name for name, backend in rack.items()
                  if getattr(backend, "docked_file", False)
                  or bool(getattr(backend, "manifest", None))]
        return _config_surface_module().membership_fields(
            self._config, sorted(built), docked)

    def _dock_want_field_now(self) -> dict:
        """The dock_want select (T-036 Addendum (2)) — choices come from the
        machine's CURRENT offer list (its own dedup filtering stays: the
        provider never re-filters). Offers unavailable in this process →
        the honest field with the one none choice."""
        offers = []
        try:
            offers = self._docking_module().catalog_offers()
        except Exception as exc:
            _LOG.info("memrack dock offers unavailable here (%s: %s)",
                      type(exc).__name__, exc)
        return _config_surface_module().dock_want_field(offers)

    def __init__(self, config=None):
        self._config = dict(config or {})
        self._session_id = ""
        self._hermes_home = None
        self._agent_context = None
        self._rack: dict = {}
        self._cards: dict = {}
        self._parked: list = []
        self._load_errors: list = []
        self._gaps: list = []
        self._active = None
        self._force = False
        self._route_log = None
        self._dock_want_outcome = None
        self._uninstalled_report = None
        self._lock = threading.Lock()
        # live membership (the runtime-switch law): every warm-initialized
        # instance this session has EVER held, kept so a park only leaves the
        # ACTIVE rack (restore = the cached instance back, never a re-probe)
        self._warm_rack: dict = {}
        # the membership memo: a hash of the last stand actually applied to
        # the rack + a cheap stat fingerprint of the stores (unchanged = the
        # per-call re-evaluation costs two stats and a directory listing)
        self._applied_membership_hash = None
        self._membership_stat_memo = None
        # the initialize kwargs the lazy (re)initialization of a member needs
        self._init_kwargs: dict = {}

    # ── lifecycle ──
    def _profile_config(self, hermes_home) -> dict:
        """`plugins.memrack.*` from the PROFILE config file — the provider is loaded
        config-less at register time (the manager injects nothing); the
        file is the live source (the wiki-memory convention).
        Second source = the DESKTOP-declared surface (storage=flat_json,
        `<hermes_home>/<name>/config.json`, 0600) — the UI writes ONLY there;
        its keys win (freshest user intent), yaml-only keys remain."""
        merged: dict = {}
        try:
            import yaml
            raw = yaml.safe_load((Path(hermes_home) / "config.yaml").read_text(encoding="utf-8")) or {}
            merged.update(dict((raw.get("plugins") or {}).get(PLUGIN_NAME) or {}))
        except Exception:
            pass          # broken yaml must not kill the desktop config
        try:
            import json
            flat = _hermes_home(hermes_home) / PLUGIN_NAME / "config.json"
            if flat.is_file():
                data = json.loads(flat.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    merged.update({k: v for k, v in data.items() if k})
        except Exception as exc:
            _LOG.warning("memrack flat_json config unreadable (%s): %s", flat.parent, exc)
        return merged

    def save_config(self, values, hermes_home=None):
        """Setup-consumer contract — the wizard hands values back; write the
        SAME block _profile_config reads (``plugins.memrack`` in config.yaml,
        wiki-memory convention). MERGE per key, not replace: keys the wizard
        did not send (search_top_k, embedding_model, ...) survive a partial
        write; sent keys override. The write stays ATOMIC and 0600 in BOTH
        modes: the HOST's own writers when the host tree is importable
        (``hermes_cli.config.save_config`` for the config.yaml block —
        merge_existing deep-merges the on-disk raw under this write, so no
        un-omitted section drops — and the host's ``atomic_json_write`` for
        the flat json sidecar), the guarded in-plugin writers otherwise
        (tmp sibling in the same directory, 0600, os.replace). Failure =
        warning, never a setup crash.

        T-036 Addendum (1) — the toggle write-path funnels HERE (both
        surfaces save through it): `<name>_in_rack` keys fold CONSISTENTLY
        before the stores are written, `_norm_backends` stays the
        source-of-truth. Built-in names fold into `enabled_backends` (the
        config-surface update: dedupe, base-db floor absorbed, wiki stays
        parked); docked names fold into the CARD FILE's presence (the
        established disabled-<name>.yaml park/unpark) and their key is then
        dropped from the written config — presence is the docked state, never
        a config key. A failing fold is a warning, the raw values still
        reach the stores."""
        values = self._fold_membership_toggles(values, hermes_home)
        home = _hermes_home(hermes_home)
        config_path = home / "config.yaml"
        try:
            import yaml
            existing: dict = {}
            if config_path.exists():
                existing = yaml.safe_load(
                    config_path.read_text(encoding="utf-8-sig")) or {}
            if not isinstance(existing, dict):
                existing = {}          # non-dict root → start clean, never crash
            plugins = existing.get("plugins")
            if not isinstance(plugins, dict):
                plugins = {}           # `plugins: null`/scalar → fresh mapping
                existing["plugins"] = plugins
            prior = plugins.get(PLUGIN_NAME)
            if not isinstance(prior, dict):
                prior = {}
            merged = {**prior, **dict(values or {})}   # merge per key
            host_yaml = _host_yaml_writer(config_path.parent)
            plugins[PLUGIN_NAME] = merged   # the block lands in both write modes
            if host_yaml is not None:
                # the host's own writer (home-bound: the resolver only fires
                # when the host context's home IS this home); the block pins
                # its subtree so default-stripping can never eat the keys
                host_yaml({"plugins": {PLUGIN_NAME: merged}},
                          merge_existing=True,
                          preserve_keys={("plugins", PLUGIN_NAME)})
                try:
                    readback = (yaml.safe_load(
                        config_path.read_text(encoding="utf-8-sig")) or {})
                    readback = (readback.get("plugins") or {}).get(PLUGIN_NAME)
                except Exception:
                    readback = None
                if isinstance(readback, dict) and readback != merged:
                    _LOG.warning("memrack host config write normalized %s: "
                                 "read-back %r != written %r",
                                 config_path, readback, merged)
            else:
                tmp = config_path.parent / (config_path.name + ".tmp")
                tmp.write_text(
                    yaml.dump(existing, default_flow_style=False,
                              allow_unicode=True),
                    encoding="utf-8")
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, config_path)

            # Mirror: keep the desktop-panel source in sync (merge, 0600) —
            # otherwise the UI shows defaults instead of the state after a wizard setup.
            import json
            flat_dir = home / PLUGIN_NAME
            flat_dir.mkdir(parents=True, exist_ok=True)
            flat = flat_dir / "config.json"
            current = {}
            if flat.is_file():
                try:
                    current = json.loads(flat.read_text(encoding="utf-8-sig")) or {}
                except Exception:
                    current = {}
            if isinstance(current, dict):
                current.update(dict(values or {}))
                host_json = _host_json_writer()
                if host_json is not None:
                    host_json(flat, current, mode=0o600, indent=2,
                              ensure_ascii=False)
                else:
                    tmp = flat.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps(current, indent=2, ensure_ascii=False),
                        encoding="utf-8")
                    os.replace(tmp, flat)
                try:
                    os.chmod(flat, 0o600)
                except OSError:
                    pass
        except Exception as exc:
            _LOG.warning("memrack save_config failed (%s): %s",
                         config_path, exc)

    def _docked_card_names(self, hermes_home) -> set:
        """Stems of the dock cards on file (active AND parked variants) — the
        presence family for the toggle fold in save_config (a key naming a
        card governs the CARD FILE, not enabled_backends)."""
        out = set()
        dock = docked_dir(hermes_home)
        if not dock.is_dir():
            return out
        for path in sorted(list(dock.glob("*.yaml")) + list(dock.glob("*.yml"))):
            name = path.name
            if name.startswith("_"):
                continue
            stem = path.stem[len("disabled-"):] if name.startswith("disabled-") \
                else path.stem
            if stem:
                out.add(stem)
        return out

    def _fold_membership_toggles(self, values, hermes_home=None):
        """The `<name>_in_rack` fold over ONE submitted values dict (pure:
        returns the values dict the stores will receive — never the caller's
        dict). The fold runs against the CURRENT live stand (both stores read
        via _profile_config) — a partial submit carrying ONLY a toggle folds
        that toggle onto the rack's stand, it does not reset
        `enabled_backends` to defaults. Built-in names → `enabled_backends`
        via the config-surface seam (the folded stand is written once as the
        fold's output); docked-card names → the presence rename via
        set_docked_membership (key dropped, presence is the state). Persisted
        toggle keys echo the folded stand — never a lie on the next render
        (a floor-absorbed off re-renders as "true")."""
        folded = dict(values or {})
        keys = [k for k in folded
                if isinstance(k, str) and k.endswith("_in_rack")]
        if not keys:
            return folded   # nothing to fold — no store read, no writes
        try:
            cs = _config_surface_module()
            carry = dict(self._profile_config(_hermes_home(hermes_home)))
            docks = self._docked_card_names(_hermes_home(hermes_home))
            folded_builtins = False
            for key in sorted(keys):
                name = key[: -len("_in_rack")].strip()
                if not name:
                    continue
                state = folded.get(key)
                on = _truthy(state)
                if name in docks:
                    report = cs.set_docked_membership(name, on,
                                                      hermes_home=hermes_home)
                    if isinstance(report, dict) and report.get("error"):
                        _LOG.warning("memrack docked membership toggle %s: %s",
                                     name, report.get("error"))
                    folded.pop(key, None)   # presence is the state, not a key
                else:
                    carry = cs.merge_membership_toggle(carry, key, state)
                    folded[key] = ("true" if name in _norm_backends(
                        carry.get("enabled_backends")) else "false")
                    folded_builtins = True
            if folded_builtins:
                # the folded stand is THE consistent write of this submission
                folded["enabled_backends"] = carry.get("enabled_backends",
                                                       folded.get("enabled_backends"))
        except Exception as exc:
            _LOG.warning("memrack toggle fold skipped (%s: %s)",
                         type(exc).__name__, exc)
        return folded

    # ── the initialize side-effect block's cross-process exclusivity ─────────
    @contextlib.contextmanager
    def _side_effect_lock(self):
        """Cross-process exclusivity for the initialize side-effect block:
        the fold, the uninstall grace and the dock_want consumption park/
        restore card files and rewrite BOTH config stores — parallel runs
        (cron, gateway, CLI, a second chat) must not race on the same
        files. One ``fcntl.flock`` on ``plugin-data/memrack/init.lock``
        with a generous 10 s budget (small retry polls); read paths stay
        unlocked. Honest degrades: a lockless platform or an unwritable
        directory yields NO exclusivity (initialize never dies — the
        Basisfall duty), and a home still busy after the budget runs its
        side effects SKIPPED this initialize (every one of them is
        idempotent and re-runs at the next one) with the honest note."""
        held = False
        handle = None
        try:
            import fcntl
        except ImportError:
            fcntl = None
        if fcntl is not None:
            try:
                lock_dir = Path(self._hermes_home) / "plugin-data" / PLUGIN_NAME
                lock_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(lock_dir, 0o700)
                except OSError:
                    pass
                handle = open(lock_dir / "init.lock", "a")
                deadline = time.monotonic() + _SIDE_EFFECT_LOCK_TIMEOUT_S
                while True:
                    try:
                        fcntl.flock(handle.fileno(),
                                    fcntl.LOCK_EX | fcntl.LOCK_NB)
                        held = True
                        break
                    except BlockingIOError:
                        pass
                    except OSError:
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_SIDE_EFFECT_LOCK_POLL_S)
            except OSError:
                held = False   # an unwritable home keeps its initialize
        if handle is None:
            yield False
            return
        try:
            yield held
        finally:
            if held:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                handle.close()
            except Exception:
                pass

    def _fold_persisted_membership_toggles(self):
        """T-037 acting law — the DECLARED surface's raw toggle keys act per
        /new. The desktop's flat writer has no provider-side fold: a gear
        pane toggle lands as a RAW ``<name>_in_rack`` key in the flat store.
        Here (before the rack discovery) the EXISTING T-036 fold runs over
        the persisted keys against the CURRENT live stand — built-in names
        fold into ``enabled_backends`` (dedupe, floor absorbed, wiki parked
        by default), docked names park/restore their CARD FILE (presence is
        the state; the key leaves the live config). The fold READS the
        stores and this consumption point never WRITES them (no hidden
        churn: the same persisted keys fold to the same stand on every
        initialize; any later provider save re-echoes the folded stand
        through the save path's laws). A fold failure is a warning — the
        initialize keeps its raw keys and racks on (the Basisfall duty)."""
        persisted = {k: v for k, v in self._config.items()
                     if isinstance(k, str) and k.endswith("_in_rack")}
        if not persisted:
            return None            # nothing to fold — no store read, no writes
        try:
            folded = self._fold_membership_toggles(dict(persisted),
                                                   self._hermes_home)
        except Exception as exc:
            _LOG.warning("memrack persisted toggle fold skipped (%s: %s)",
                         type(exc).__name__, exc)
            return None
        for key in persisted:
            if key not in folded:  # docked members: presence is the state
                self._config.pop(key, None)
        self._config.update(folded)   # the folded stand + the echoed keys
        return folded

    def initialize(self, session_id="", **kwargs):
        self._session_id = session_id or ""
        self._hermes_home = str(_hermes_home(kwargs.get("hermes_home")))
        kwargs.pop("hermes_home", None)  # we own this argument; no duplicate kwarg to backends
        # the automatic-write context (the ABC contract: initialize kwargs may
        # carry ``agent_context``): recorded once here, consulted by the
        # memory-MIRROR write path only (cron/subagent runs stay writers-free;
        # tool writes are decisions and stay unaffected).
        self._agent_context = kwargs.get("agent_context")
        # the kwargs the lazy member (re)initialization needs (the LIVE
        # membership rebuild mirrors the init fan-out exactly on a
        # never-initialized member)
        self._init_kwargs = dict(kwargs)
        self._config.update(self._profile_config(self._hermes_home))   # the file wins the live keys

        # a re-initialize starts CLEAN: no stale gaps from a previous session, no stale force
        self._gaps = []
        self._force = False
        self._dock_want_outcome = None

        # the route log lands FIRST (the side-effect block's honest notes
        # ride it too): the plugin-data dir is idempotent and private; a
        # broken home degrades to no log (the initialize racks on)
        log_dir = Path(self._hermes_home) / "plugin-data" / PLUGIN_NAME
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(log_dir, 0o700)
        except OSError:
            pass
        self._route_log = log_dir / "route_log.jsonl"

        # ── the side-effect block, cross-process serialized ────────────────
        # The fold, the uninstall grace and the dock_want consumption park/
        # restore card files and rewrite BOTH config stores; parallel runs on
        # the same home serialize behind one flock here (read paths stay
        # unlocked — the atomic tmp/replace discipline carries them). The
        # store stand re-reads UNDER the lock, so a serialized runner keys on
        # the winner's stand, never on its pre-lock snapshot.
        with self._side_effect_lock() as side_effects_exclusive:
            self._config.update(self._profile_config(self._hermes_home))
            if not side_effects_exclusive:
                _LOG.warning(
                    "memrack init side-effect lock not held (home=%s) — "
                    "fold/uninstall grace/dock_want skipped this initialize; "
                    "they re-run idempotently at the next one",
                    self._hermes_home)
                try:
                    self._log_route(
                        "init_side_effects",
                        "lock not held — side effects skipped this initialize",
                        "—")
                except Exception:
                    pass
            else:
                # T-037 acting law — the declared gear pane's persisted raw
                # toggles act per /new BEFORE the discovery: the SAME fold
                # the save path runs (built-ins → enabled_backends; docked
                # names park/restore their card file), so a restored card
                # racks in THIS initialize.
                self._fold_persisted_membership_toggles()
                # the UNINSTALL-GRACE, consumed BEFORE the discovery — a docked card
                # whose provider install left the house parks immediately (honest
                # uninstalled-at flag + kept-until note, NO dock-error spam: the
                # parked manifest never reaches the rack), a re-installed flagged
                # card restores without a re-probe, and the retention group removes
                # long-expired cards with a per-file backup. A failure is a
                # warning — the initialize racks on (the Basisfall duty).
                try:
                    self._uninstalled_report = _config_surface_module() \
                        .uninstall_grace_hook(self._config, hermes_home=self._hermes_home)
                    grace = self._uninstalled_report
                    if isinstance(grace, dict):
                        if grace.get("parked") or grace.get("removed") \
                                or grace.get("restored") or grace.get("switch_off"):
                            _LOG.info(
                                "memrack uninstall grace: parked=%s restored=%s "
                                "removed=%s switch_off=%s",
                                [p.get("provider") for p in grace.get("parked", [])],
                                [p.get("provider") for p in grace.get("restored", [])],
                                [p.get("provider") for p in
                                 grace.get("removed", [])],
                                [p.get("provider") for p in
                                 grace.get("switch_off", [])])
                        if grace.get("error"):
                            _LOG.warning("memrack uninstall grace failed: %s",
                                         grace["error"])
                except Exception as exc:
                    self._uninstalled_report = {
                        "error": "%s: %s" % (type(exc).__name__, exc)}
                    _LOG.warning("memrack uninstall grace hook failed: %s: %s",
                                     type(exc).__name__, exc)
                # the concurrent-consume guard: a serialized runner whose
                # pre-lock snapshot carried the choice but whose fresh store
                # stand now carries only the one-shot receipt = ANOTHER
                # initialize consumed it while we waited for the lock — never
                # a second probe (the stores are the shared truth).
                choice_live = str(self._config.get("dock_want") or "").strip()
                receipt = self._config.get("dock_want_last")
                if choice_live and isinstance(receipt, dict) \
                        and str(receipt.get("provider") or "").strip() == \
                        choice_live \
                        and "dock_want" not in (self._profile_config(
                            self._hermes_home) or {}):
                    self._config.pop("dock_want", None)
                    _LOG.info("memrack dock_want: %r left the stores while "
                              "this initialize waited on the lock — the "
                              "receipt is on file (already consumed)",
                              choice_live)
                    self._log_route(
                        "dock_want",
                        "concurrent initialize already consumed %r (the "
                        "receipt is in both stores) — skipped" % choice_live,
                        "—")
                # T-036 Addendum (2), the consumption point: a set `dock_want` select
                # runs the EXISTING docking machine here (probe + draft card + shim —
                # the config choice IS the user decision, no heimliches Auto-Dock).
                # It runs BEFORE the discovery below, so the fresh card racks by
                # presence in THIS initialize. The receipt (or the machine's honest
                # refusal) surfaces via status `dock_want_receipt`; a hook failure is
                # an honest receipt, never an initialize crash (the Basisfall duty).
                try:
                    self._dock_want_outcome = _config_surface_module().dock_want_hook(
                        self._config, hermes_home=self._hermes_home,
                        docking=self._docking_module())
                    outcome = self._dock_want_outcome
                    if isinstance(outcome, dict):
                        if outcome.get("refused") or outcome.get("error"):
                            _LOG.warning("memrack dock_want %r: %s",
                                         outcome.get("status"),
                                         outcome.get("gap") or outcome.get("error"))
                        else:
                            _LOG.info("memrack dock_want consumed: %s (%s)",
                                      outcome.get("status"), outcome.get("card_file"))
                except Exception as exc:
                    self._dock_want_outcome = {
                        "refused": True,
                        "error": "dock_want hook failed: %s: %s"
                                 % (type(exc).__name__, exc)}
                    _LOG.warning("memrack dock_want hook failed: %s", exc)

        enabled = self._enabled_builtins_from_config()
        rack = discover_backends(self._hermes_home, enabled_builtins=enabled)
        self._rack = rack["backends"]
        self._parked = rack["parked"]
        self._load_errors = rack["errors"]
        self._cards = self._cards_now()

        # initialize every rack member; a member that fails to init = Gap (degradation ladder),
        # but the DEFAULT/FORCED target failing is fatal (Basisfall duty).
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

        # cards rebuilt post-init (vec probe happens inside init)
        self._cards = self._cards_now()

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
        # the live membership memo starts AT the init stand (the per-call
        # re-evaluation compares against this, and the warm cache starts
        # full: a mid-session park keeps its initialized instance around)
        self._warm_rack = dict(self._rack)
        self._applied_membership_hash = self._membership_stand_hash()
        self._membership_stat_memo = self._membership_stat_fingerprint()
        # the cards-on-delta memo starts EMPTY: the first tool answer of this
        # provider instance carries the full cards (the plan's first-call rule)
        self._cards_served_stand = None
        _LOG.info("memrack ready (v%s): backends=%s active=%s gaps=%s parked=%s errors=%s",
                  __version__, sorted(self._rack),
                  self._active.name if self._active else None,
                  self._gaps or "none", self._parked or "none",
                  self._load_errors or "none")

    def _enabled_builtins_from_config(self):
        """The enabled built-ins stand, ONE copy (the init fan-out and the
        live membership rebuild share it): the normalized list or the
        defaults, the floor always mounted, the activation auto-provision
        merged (never a disable)."""
        enabled = _norm_backends(self._config.get("enabled_backends"))
        if not enabled:
            enabled = list(_DEFAULT_ENABLED)
        if DEFAULT_BACKEND not in enabled:   # the floor mounts with >=1 non-base entry already
            enabled = [DEFAULT_BACKEND] + enabled
        if _truthy(self._config.get("auto_enable")):   # activation auto-provision —
            for b in ("base-db", "wiki"):              # merge only, never a disable
                if b not in enabled:
                    enabled.append(b)
        return enabled

    def _pick_default_target(self):
        forced = self._config.get("backend")
        target = forced or DEFAULT_BACKEND
        if target not in self._rack and len(self._rack) == 1:
            # Basisfall rule: single-backend rack → go there, whatever its name
            target = next(iter(self._rack))
        return target

    def _membership_stat_fingerprint(self):
        """The cheap per-call fingerprint over the membership stands: the
        stat identity of BOTH config stores plus the docked-card presence
        family. Unchanged fingerprint = the re-evaluation costs the stats
        only (no store read, no fold, no rebuild)."""
        home = Path(self._hermes_home)
        parts = []
        for path in (home / "config.yaml",
                     home / PLUGIN_NAME / "config.json"):
            try:
                st = path.stat()
                parts.append([str(path), st.st_mtime_ns, st.st_size])
            except OSError:
                parts.append([str(path), None, None])
        parts.append(sorted(self._docked_card_names(home)))
        return json.dumps(parts, sort_keys=True)

    def _membership_stand_hash(self):
        """The SEMANTIC membership stand: the persisted toggle keys + the
        enabled string over both stores, plus the docked-card presence
        family (a dock card's membership IS its presence). The applied memo
        is a hash of exactly this — a store write that does not change the
        stand never triggers a fold."""
        cfg = self._profile_config(self._hermes_home) or {}
        toggles = sorted(
            (k, _truthy(v)) for k, v in cfg.items()
            if isinstance(k, str) and k.endswith("_in_rack"))
        payload = {
            "toggles": toggles,
            "enabled": _norm_backends(cfg.get("enabled_backends")),
            "docked": sorted(self._docked_card_names(self._hermes_home)),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True)
                              .encode("utf-8")).hexdigest()

    def _rebuild_rack_membership(self):
        """Rebuild the ACTIVE rack from the current stand, reusing warm
        already-initialized instances: an instance the rack has held before
        comes back by cache (park = out of the active rack, instance stays
        warm; restore = the cached instance back — no re-probe), a NEVER
        initialized backend initializes lazily here (its honest probe/gap,
        exactly like an initialize member). Cards, active target and the
        park/error surfaces follow the NEW stand; the rack_status slot
        reflects the new participation automatically (it reads the
        live cards). A rebuild NEVER touches the config stores."""
        enabled = self._enabled_builtins_from_config()
        discovered = discover_backends(self._hermes_home,
                                       enabled_builtins=enabled)
        desired = discovered["backends"]
        new_rack: dict = {}
        warm_reused, lazy_inited = [], []
        for name, backend in sorted(desired.items()):
            warm = self._warm_rack.get(name)
            if warm is not None:
                new_rack[name] = warm          # restore: the cached instance
                warm_reused.append(name)
                continue
            try:                               # never-initialized: lazy init
                backend.initialize(
                    session_id=self._session_id,
                    hermes_home=self._hermes_home,
                    provider_config=self._config, **self._init_kwargs)
                new_rack[name] = backend
                self._warm_rack[name] = backend
                lazy_inited.append(name)
            except Exception as exc:
                self._gap_once(name, "%s: %s" % (type(exc).__name__, exc))
                _LOG.warning("memrack live membership: %r failed to "
                             "initialize lazily (%s: %s)", name,
                             type(exc).__name__, exc)
        joined = sorted(set(new_rack) - set(self._rack))
        left = sorted(set(self._rack) - set(new_rack))
        self._rack = new_rack
        self._parked = discovered["parked"]
        self._load_errors = discovered["errors"]
        target = self._pick_default_target()
        self._active = self._rack.get(target) or \
            self._rack.get(DEFAULT_BACKEND)
        forced = self._config.get("backend")
        self._force = bool(forced and forced in self._rack)
        self._cards = self._cards_now()
        if joined or left:
            _LOG.info("memrack live membership: joined=%s left=%s "
                      "warm_reused=%s lazy_inited=%s",
                      joined, left, warm_reused, lazy_inited)
            self._log_route(
                "membership_rebuild",
                "joined=%s left=%s warm_reused=%s lazy_inited=%s"
                % (",".join(joined), ",".join(left),
                   ",".join(warm_reused), ",".join(lazy_inited)), "—")
        return joined, left

    def _ensure_live_membership(self):
        """Per-call membership re-evaluation (the runtime-switch law): ONE
        cheap stat fingerprint per call; a CHANGED stand runs the SAME
        flock-protected fold an initialize runs and rebuilds the rack off
        warm instances (see _rebuild_rack_membership). Unchanged stand =
        zero work. The hook serves the FOUR tool paths (search/status/
        write/vec_add) — the injected context channel (prefetch/system
        block) keeps its session stand (the channel split law). Read paths
        here never write the config stores; a flock-BUSY home is an honest
        skip note whose stand is applied at the next call (the memo does
        not advance)."""
        if self._applied_membership_hash is None:
            return          # pre-init or a fold-less init: the hook is inert
        try:
            fingerprint = self._membership_stat_fingerprint()
        except Exception as exc:
            _LOG.warning("memrack membership fingerprint unreadable (%s: %s)",
                         type(exc).__name__, exc)
            return
        if fingerprint == self._membership_stat_memo:
            return          # unchanged: zero work beyond the fingerprint
        with self._side_effect_lock() as exclusive:
            if not exclusive:
                # a serialized runner holds the stores — honest skip, the
                # memo stays put, the next call retries the stand
                _LOG.info("memrack membership fold skipped: the home's "
                          "side-effect lock is busy (stand re-applies at "
                          "the next tool call)")
                self._log_route("membership_rebuild",
                                "lock busy — fold skipped this call", "—")
                return
            fingerprint = self._membership_stat_fingerprint()
            if fingerprint == self._membership_stat_memo:
                return      # a serialized runner already applied this stand
            stand = self._membership_stand_hash()
            if stand == self._applied_membership_hash:
                # the stores moved (an unrelated key) but the MEMBERSHIP
                # stand did not: memoize the new fingerprint, fold nothing
                self._membership_stat_memo = fingerprint
                return
            self._config.update(self._profile_config(self._hermes_home))
            self._fold_persisted_membership_toggles()
            self._rebuild_rack_membership()
            self._applied_membership_hash = self._membership_stand_hash()
            self._membership_stat_memo = self._membership_stat_fingerprint()

    # the four tool paths the runtime-switch law serves
    _LIVE_MEMBERSHIP_TOOLS = ("memrack_search", "memrack_write",
                              "memrack_vec_add", "memrack_status")

    def _cards_now(self) -> dict:
        """Rack cards rebuilt ON CALL from the live rack (the same logic the
        init uses) — tool answers never serve the init-time `self._cards`
        snapshot, so a card can never go stale within a session."""
        rack = getattr(self, "_rack", None) or {}
        return {n: _card(b) for n, b in rack.items()}

    def _remember_answered_stand(self):
        """Memoize the membership stand the LAST card-carrying answer was
        built on (ONE source: the same stand hash the live-membership law
        uses). The next search compares against this: unchanged stand = the
        compact marker, changed stand = the full cards again. Write and
        vec_add answers (which keep their own unchanged form) advance the
        memo too, so they never produce a duplicate card prose."""
        self._cards_served_stand = self._membership_stand_hash()

    @staticmethod
    def _rack_status_slot(cards, ran=None, skipped=None, engine_errors=None, reason=None) -> dict:
        """The lean rack-status slot: per ACTIVE backend its live modes (the SAME
        source as the cards — one copy, no second truth) plus `participation`
        for THIS query: searched | skipped (mode not carried, short reason) |
        gap (an engine_errors entry). Deterministic order (sorted names), NO
        card texts (the token law). Status = the state question, request routing
        stays the query's own business: the slot always shows the WHOLE rack."""
        ran = ran or {}
        skipped = skipped if isinstance(skipped, dict) else {}
        reason = reason if isinstance(reason, str) else (None if reason is None else str(reason))
        err_names = {e.get("backend") for e in (engine_errors or [])}
        out: dict = {}
        for name in sorted(cards):
            entry = {"modes": list(cards[name].get("modes") or ())}
            if name in err_names:
                entry["participation"] = "gap"
            elif ran.get(name):
                entry["participation"] = "searched"
            else:
                entry["participation"] = "skipped"
                if isinstance(skipped, str):   # a whole-slot rejection reason
                    reason = skipped
                if reason:
                    entry["reason"] = reason
                elif name in skipped:
                    entry["reason"] = skipped[name]
            out[name] = entry
        return out

    def _gap_once(self, backend, error):
        """Append a gap entry ONCE (assembly paths may run per call; duplicate
        honest notes are noise, not signal). Entry shape = the init-gap shape."""
        entry = {"backend": backend, "error": error}
        if entry not in self._gaps:
            self._gaps.append(entry)

    # ── routing bookkeeping ──
    def _routed_for_kind(self, kind: str):
        """Source-rule routing (deterministic, no content judgement):
        forced > rules: episodic/turn → wiki (session-writer) if enabled else None;
        explicit/semantic/curation → the default floor backend."""
        if self._force:
            return self._active
        if kind == EPISODIC:
            return self._rack.get("wiki")   # session-writer, or None (state.db is canonical)
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
        out = None
        if backend is None:
            # Turns live in Hermes' state.db; memrack only records the routing fact.
            self._log_route("turn", "unstored (state.db is canonical)", "—")
        else:
            out = backend.on_turn(user_content, assistant_content,
                                  session_id=session_id or self._session_id)
            self._log_route("turn", "episodic", backend.name)
        # the opt-in per-card turn gate (canon-keeping): the episodic route
        # above is UNCHANGED — without the gate key a dock receives nothing,
        # exactly as before (no silent copying of the canonical turn). A
        # member whose card records `forward_turns` (the human-confirmed
        # decision, forward_writes' sibling) receives the event through an
        # isolated fan-out; a failing member is logged and the ROUTED
        # receipt stays THE answer (single-result form, never merged).
        for bname, member in list((getattr(self, "_rack", None) or {}).items()):
            if member is backend:
                continue   # the routed delivery already happened (no double)
            man = getattr(member, "manifest", None)
            if not isinstance(man, dict) or not _truthy(man.get("forward_turns")):
                continue
            fn = getattr(member, "on_turn", None)
            if not callable(fn):
                continue   # the optional-hook contract: a silent skip
            try:
                fn(user_content, assistant_content,
                   session_id=session_id or self._session_id)
            except Exception as exc:
                _LOG.warning("memrack turn forward to %s failed: %s", bname, exc)
            self._log_route("turn", "forward-gated", bname)
        return out

    def on_memory_write(self, action, target, content, metadata=None, *,
                        session_id="", **kwargs):
        # the memory-MIRROR suppression (the ABC's automatic-write law):
        # automatic contexts (cron/subagent) receive no mirrored writes —
        # this path is the host memory tool's mirror, while explicit
        # memrack_write tool calls stay the agent's decision. An
        # unrecorded/unknown context rides exactly as today.
        context = kwargs.get("agent_context",
                             getattr(self, "_agent_context", None))
        if str(context or "").strip().lower() in _MEMRACK_MIRROR_SKIP_CONTEXTS:
            note = ("agent_context=%s — the memory mirror is suppressed for "
                    "automatic contexts" % context)
            self._log_route("memory_write:skip %s" % action,
                            "%s (%s)" % (target, note), "—")
            _LOG.info("memrack mirror write skipped (%s): %s", action, note)
            return {"status": "skipped", "reason": note}
        backend = self._routed_for_kind(EXPLICIT)
        if backend is None:
            return {"status": "unrouted", "reason": "no explicit-facts backend in rack"}
        out = backend.on_memory_write(action, target, content, metadata=metadata,
                                      session_id=session_id or self._session_id)
        self._log_route("memory_write:%s" % action, str(target), backend.name)
        # catalog docks with a RECORDED forward decision receive the write
        # mirror too (the docking machine only records it when the write
        # semantics are compatible; docks whose card says nothing receive
        # nothing — fail-closed). The routed receipt stays THE answer; a
        # failing mirror forward is logged, never merged into it.
        for bname, member in list(self._rack.items()):
            if member is backend:
                continue
            man = getattr(member, "manifest", None)
            if not isinstance(man, dict) or not _truthy(man.get("forward_writes")):
                continue
            fn = getattr(member, "on_memory_write", None)
            if not callable(fn):
                continue
            try:
                fn(action, target, content, metadata=metadata,
                   session_id=session_id or self._session_id)
            except Exception as exc:
                _LOG.warning("memrack memory-write forward to %s failed: %s",
                             bname, exc)
        return out

    # ── provider hooks (T-031, decision (a): fan-out over the whole rack) ──
    def _fanout_timeout_s(self) -> float:
        """The per-backend hook budget (``fanout_timeout_s``, default 5 s):
        a hanging engine becomes an honest gap once its budget runs out,
        while the rest of the rack still delivers. Non-numbers and values
        <= 0 fall back to the declared default."""
        raw = (getattr(self, "_config", None) or {}).get("fanout_timeout_s")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 5.0
        return value if value > 0 else 5.0

    def _prefetch_max_chars(self) -> int:
        """The merged prefetch block's total character budget
        (``prefetch_max_chars``, default 4000); non-numbers and values <= 0
        fall back to the declared default."""
        raw = (getattr(self, "_config", None) or {}).get("prefetch_max_chars")
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return 4000
        return value if value > 0 else 4000

    @staticmethod
    def _spawn_worker(target, name):
        """Launch ONE hook worker: the host's ``spawn_context_thread`` when
        the host tree is importable (it binds the spawner's contextvars, so
        the profile scope travels with the worker — the host contract for
        every memory-provider background job), a plain daemon thread
        otherwise (hermes-less runs, this suite)."""
        try:
            from agent.memory_provider import spawn_context_thread
        except Exception:
            return threading.Thread(target=target, name=name, daemon=True)
        return spawn_context_thread(target, name=name, daemon=True)

    def _hook_fanout(self, hook, *args, _payload_fn=None, **kwargs):
        """Fan a provider hook out to EVERY rack backend, in PARALLEL: each
        member runs in its own worker thread behind a per-backend budget
        (``fanout_timeout_s``, default 5 s) — a hanging engine becomes an
        honest gap the moment its budget runs out and the rest of the rack
        still delivers. Results are collected deterministically in RACK
        order (never completion order); a member's failure stays isolated
        (per-backend error capture — the search fan-out law) and a backend
        without the hook is a silent skip (the optional-hook contract, no
        warn spam). ``_payload_fn`` lets a caller reshape the per-member
        arguments (the session-end transcript gate); it is CONSUMED here
        and never reaches a backend hook."""
        rack = list((getattr(self, "_rack", None) or {}).items())
        if not rack:
            return [], []
        timeout = self._fanout_timeout_s()
        slots: list = [_HOOK_UNSET] * len(rack)
        slot_locks = [threading.Lock() for _ in rack]

        def _store(index, entry) -> bool:
            """First-write-wins slots: a LATE finisher can never overwrite a
            collected result or the gap law (the collector's budgeted gap
            write below stays the one unconditional write)."""
            with slot_locks[index]:
                if slots[index] is _HOOK_UNSET:
                    slots[index] = entry
                    return True
                return False

        def _capture(index, name, backend, call_args, call_kwargs):
            try:
                value = getattr(backend, hook, None)(*call_args, **call_kwargs)
            except Exception as exc:
                _store(index, ("err", {"backend": name,
                                       "error": "%s: %s" % (type(exc).__name__, exc)}))
                _LOG.warning("memrack backend %s hook %s failed: %s",
                             name, hook, exc)
            else:
                _store(index, ("res", value))

        workers = []
        for index, (name, backend) in enumerate(rack):
            fn = getattr(backend, hook, None)
            if not callable(fn):
                continue   # the optional-hook contract: a silent skip
            call_args, call_kwargs = args, kwargs
            if _payload_fn is not None:
                try:
                    call_args, call_kwargs = _payload_fn(backend, args, kwargs)
                except Exception as exc:
                    slots[index] = ("err", {"backend": name,
                                            "error": "payload gate failed: "
                                                     "%s: %s" % (type(exc).__name__, exc)})
                    _LOG.warning("memrack backend %s hook %s payload gate "
                                 "failed: %s", name, hook, exc)
                    continue
            thread = self._spawn_worker(
                lambda i=index, n=name, b=backend, ca=call_args,
                ck=call_kwargs: _capture(i, n, b, ca, ck),
                name="memrack-%s-%s" % (hook, name))
            thread.start()
            workers.append((index, name, thread))
        for index, name, thread in workers:
            thread.join(timeout)
            if slots[index] is _HOOK_UNSET:
                # the per-backend budget ran out: the honest gap, written
                # unconditionally so a worker finishing microseconds later
                # can never retroactively claim the delivery
                slots[index] = ("err", {
                    "backend": name,
                    "error": "hook %r timed out after %ss (fanout_timeout_s) "
                             "— result not collected" % (hook, timeout)})
                _LOG.warning("memrack backend %s hook %s exceeded its %ss "
                             "fan-out budget — result not collected",
                             name, hook, timeout)
        results, engine_errors = [], []
        for index, (name, _backend) in enumerate(rack):
            outcome = slots[index]
            if outcome is _HOOK_UNSET:
                continue   # the rack member never took this hook
            if outcome[0] == "res":
                results.append((name, outcome[1]))
            else:
                engine_errors.append(outcome[1])
        return results, engine_errors

    @staticmethod
    def _pack_fanout(results, engine_errors):
        """Aggregation for the dict-shaped hooks: None answers are the no-op
        receipt, not an Ergebnis; >1 real result → {"backend": result}
        (engine_errors ride along, the search-fanout shape); exactly one real
        result keeps the pre-fanout form (no breaking — even when a SIBLING
        engine erred, that error is isolated + logged); none → the old None."""
        real = [(name, out) for name, out in results if out is not None]
        if not real:
            return None
        if len(real) == 1:
            return real[0][1]
        packed = dict(real)
        if engine_errors:
            packed["engine_errors"] = engine_errors
        return packed

    def on_session_end(self, messages=None, **kwargs):
        # the per-card transcript gate rides the fan-out's payload step:
        # docked members carry the transcript only on their recorded
        # forward_transcript decision, built-ins byte-identical — the
        # routing, isolation and packing stay exactly as before.
        results, engine_errors = self._hook_fanout(
            "on_session_end", messages, session_id=self._session_id,
            _payload_fn=_session_end_payload)
        reached = [name for name, _ in results] + [e["backend"] for e in engine_errors]
        if reached:
            self._log_route("session_end", "fan-out", ", ".join(sorted(reached)))
        return self._pack_fanout(results, engine_errors)

    def on_session_switch(self, new_session_id=None, **kwargs):
        results, engine_errors = self._hook_fanout(
            "on_session_switch", new_session_id, **kwargs)
        return self._pack_fanout(results, engine_errors)

    def on_pre_compress(self, messages=None, **kwargs):
        results, engine_errors = self._hook_fanout(
            "on_pre_compress", messages, **kwargs)
        return self._pack_fanout(results, engine_errors)

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
        results, engine_errors = self._hook_fanout(
            "prefetch", query, session_id=session_id or self._session_id)
        # the str contract has no error slot — errors stay isolated + logged above
        parts = []
        for _name, out in results:
            if not out:
                continue
            if isinstance(out, (list, tuple)):
                parts.extend(str(p) for p in out if p)  # a list return flattens into the merge
            else:
                parts.append(out)
        merged = "\n\n".join(parts)
        budget = self._prefetch_max_chars()
        if budget and len(merged) > budget:
            # the honest total-budget law: the concatenation truncates AT the
            # budget and names what was left out (the stable rack order stays)
            merged = "%s\n\n[+%d chars truncated (prefetch_max_chars)]" % (
                merged[:budget], len(merged) - budget)
        return merged

    def system_prompt_block(self) -> str:
        results, engine_errors = self._hook_fanout("system_prompt_block")
        parts, seen = [], set()
        for _name, out in results:
            block = str(out or "").strip()
            if block and block not in seen:     # only non-empty blocks, dedupe
                seen.add(block)
                parts.append(block)
        card_lines = self._card_block()
        if card_lines:
            parts.append(card_lines)
        return "\n\n".join(parts)

    def _card_block(self) -> str:
        """The rack card (the routing-law surface): one line per backend, informs the agent's choices."""
        if not self._rack:
            return ""
        lines = ["memrack rack (v%s): pick write destinations deliberately in memrack_write;" % __version__]
        for bname in sorted(self._cards):
            c = self._cards[bname]
            extra = ""
            if c.get("declared"):
                extra = " | declared-without-shipping: %s" % ",".join(c["declared"])
            lines.append("- %s: %s | search: %s%s%s%s" % (
                bname, c["best_for"] or "",
                ",".join(c["modes"]) or "none",
                "" if c["write"] else " | no atomic write",
                " (built-in)" if c["built_in"] else "",
                extra))
        if len(lines) > 9:
            overflow = len(self._cards) - 8
            lines = lines[:9] + ["- … +%d more backends — memrack_status" % overflow]
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

    # ── tools: search / write / status (the retrieval tools, §1.2) ──
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
            # catalog docks (mirror-not-rebuild): a docked provider's TOOL schemas
            # surface on the rack under their MIRRORED memrack-namespace names —
            # the provider's original tool names never reach the host (mirror law).
            for bname, backend in (self._rack or {}).items():
                if self._active is backend:
                    continue
                prop = getattr(backend, "tool_schemas", None)
                if not callable(prop):
                    continue
                try:
                    member_schemas = list(prop() or [])
                except Exception as exc:
                    _LOG.warning("memrack backend %s tool mirror unreadable: %s",
                                 bname, exc)
                    self._gap_once(bname, "tool mirror unreadable: %s: %s"
                                   % (type(exc).__name__, exc))
                    continue
                for schema in member_schemas:
                    sname = _schema_name(schema)
                    if not sname or sname in seen:
                        continue
                    seen.add(sname)
                    schemas.append(schema)
        our = {
            "memrack_search": {
                "type": "object", "properties": {
                    "query": {"type": "string"},
                    "mode": {"type": "string",
                             "enum": list(_SEARCH_MODES) + ["all"],
                             "default": "all"},
                    "backend": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                }, "required": ["query"],
                "additionalProperties": False,
            },
            "memrack_write": {
                "type": "object", "properties": {
                    "content": {"type": "string"},
                    "backend": {"type": "string"},
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
            "memrack_dock": {
                "type": "object", "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "offer", "show", "probe",
                                        "refresh", "remove"],
                               "default": "list"},
                    "provider": {"type": "string"},
                }, "required": [], "additionalProperties": False,
            },
            "memrack_vec_add": {
                # T-047: ingest into the vec store's OWN corpus (document chunks).
                "type": "object", "properties": {
                    "source_ref": {"type": "string",
                                   "description": "stable reading reference, e.g. a "
                                                  "wiki path or a file path"},
                    "text": {"type": "string"},
                    "path": {"type": "string",
                             "description": ".md/.txt read to text, .pdf via pypdf "
                                            "(optional dep; fail loud when missing)"},
                    "chunk_chars": {"type": "integer", "default": 1200},
                    "chunk_overlap": {"type": "integer", "default": 0},
                    "pages": {"type": "integer",
                              "description": "pdf-only: limit to the first N pages"},
                }, "required": ["source_ref"], "additionalProperties": False,
            },
            "memrack_card_review": {
                "type": "object", "properties": {
                    "backend": {"type": "string",
                                "description": "the DOCKED backend's card name"},
                    "best_for": {"type": "string",
                                 "description": "the reviewed, final best_for text"},
                    "limits": {"type": "string",
                               "description": "the reviewed, final limits text"},
                }, "required": ["backend", "best_for", "limits"],
                "additionalProperties": False,
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
        if tool_name in self._LIVE_MEMBERSHIP_TOOLS:
            # the runtime-switch law: these four paths see the rack live
            # (the injected context channel follows the session instead)
            self._ensure_live_membership()
        if tool_name == "memrack_search":
            return self._tool_search(args)
        if tool_name == "memrack_write":
            return self._tool_write(args)
        if tool_name == "memrack_import":
            return self._tool_import(args)
        if tool_name == "memrack_vec_add":
            # T-047: ingest into the vec store's OWN corpus (document chunks).
            return json.dumps(self._tool_vec_add(args), indent=2, ensure_ascii=False)
        if tool_name == "memrack_dock":
            return json.dumps(self._tool_dock(args), indent=2, ensure_ascii=False)
        if tool_name == "memrack_card_review":
            # T-036 Addendum (4): the chat-confirm review writer — the
            # controlled errors are ANSWERS (honest {"error": ...}), the
            # surface never crashes on one.
            return json.dumps(self._tool_card_review(args), indent=2,
                              ensure_ascii=False)
        if tool_name == self._status_tool_name():
            return json.dumps(self.status(), indent=2, ensure_ascii=False)
        # mirrored catalog tools: dispatch by the effective name the dock
        # registered — the provider never sees the host speak its original
        # name (the mirror law); unknown names fall through to KeyError.
        mirrored = self._mirrored_dispatch(tool_name, args, **kwargs)
        if mirrored is not _MIRROR_MISS:
            return mirrored
        if self._active is not None:
            fn = getattr(self._active, "handle_tool_call", None)
            if fn is not None:
                return fn(tool_name, args, **kwargs)
        raise KeyError("unknown memrack tool %r" % tool_name)

    # ── catalog-dock mirror dispatch + docking surface (mirror-not-rebuild) ──

    def _mirrored_index(self):
        """{effective mirrored tool name: backend} from the LIVE rack, rebuilt
        on call (no stale index — the cards' law). A name claimed by TWO
        backends is registered as the AMBIGUOUS marker: dispatch refuses it
        (fail-closed, no winner picks) and a gap note names both claimants."""
        index = {}
        for bname, backend in (self._rack or {}).items():
            prop = getattr(backend, "tool_schemas", None)
            if not callable(prop):
                continue
            try:
                member_schemas = list(prop() or [])
            except Exception as exc:
                _LOG.warning("memrack backend %s tool mirror unreadable: %s",
                             bname, exc)
                self._gap_once(bname, "mirrored tools unreadable: %s: %s"
                               % (type(exc).__name__, exc))
                continue
            for schema in member_schemas:
                sname = _schema_name(schema)
                if not sname:
                    continue
                current = index.get(sname)
                if current is not None and current is not backend:
                    index[sname] = None
                    self._gap_once(bname,
                                   "mirrored tool name collision: %r claimed by "
                                   "both %s and %s — dispatch refuses (fail-closed)"
                                   % (sname, getattr(current, "name", "?"), bname))
                elif sname not in index:
                    index[sname] = backend
        return index

    def _mirrored_dispatch(self, tool_name, args, **kwargs):
        """Route ONE call by its effective mirrored name to the docked
        provider. Unknown names return the MISS sentinel (the caller carries
        on); collisions/ambiguities return an honest error — never a
        guessed tool or a winner pick."""
        entry = self._mirrored_index().get(str(tool_name or ""))
        if entry is None:
            return _MIRROR_MISS
        backend = entry
        self._log_route("mirrored_tool", str(tool_name),
                        getattr(backend, "name", "?"))
        return backend.handle_mirrored_call(str(tool_name), args, **kwargs)

    def _docking_module(self):
        """The docking machine, spec-path resolved beside THIS file (one copy
        per process, cached in sys.modules) — no package import needed."""
        import importlib.util
        import sys
        mod = sys.modules.get("memrack_docking_core")
        if mod is None:
            spec = importlib.util.spec_from_file_location(
                "memrack_docking_core",
                Path(__file__).resolve().parent / "memrack" / "docking.py")
            if spec is None or spec.loader is None:
                raise ImportError("dock machine not resolvable beside the provider")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["memrack_docking_core"] = mod
            spec.loader.exec_module(mod)
        return mod

    def _tool_dock(self, args):
        """`memrack dock` on the tool surface: list/offer/probe/show/refresh/
        remove — the SAME machine `hermes memrack dock <sub>` drives (one
        backend, two surfaces). Everything it does is EXPLICIT (no auto-dock)."""
        action = str(args.get("action") or "list").strip().lower()
        provider = str(args.get("provider") or "").strip()
        docking = self._docking_module()
        if action == "list":
            return docking.dock_list(hermes_home=self._hermes_home)
        if not provider:
            return {"error": "provider required for action %r" % action,
                    "hint": "actions: list (no args) | offer | probe | show | "
                            "refresh | remove (+ provider)"}
        if action == "offer":
            return docking.dock(provider, hermes_home=self._hermes_home)
        if action == "probe":
            return docking.dock(provider, probe=True,
                                hermes_home=self._hermes_home)
        if action == "refresh":
            return docking.dock(provider, refresh=True,
                                hermes_home=self._hermes_home)
        if action == "show":
            return docking.dock_show(provider, hermes_home=self._hermes_home)
        if action == "remove":
            return docking.dock_remove(provider, hermes_home=self._hermes_home)
        return {"error": "unknown dock action %r" % action,
                "hint": "actions: list | offer | probe | show | refresh | remove"}

    def _tool_card_review(self, args):
        """T-036 Addendum (4): the chat-confirm review over the config-surface
        seam — reviewed best_for/limits set VERBATIM onto the docked card,
        draft cleared, one chat-confirm revision. Unknown backend / empty
        texts are the controlled error ANSWER (no crash, nothing written);
        an unexpected seam failure degrades to an honest error too."""
        try:
            out = _config_surface_module().review_card(
                args.get("backend"), args.get("best_for"), args.get("limits"),
                hermes_home=self._hermes_home)
        except Exception as exc:
            out = {"error": "card review failed: %s: %s"
                           % (type(exc).__name__, exc)}
        return out

    # ── search: fan-out + ONE fusion rule (the fusion law) ──
    def _tool_search(self, args):
        started = time.time()
        query = str(args.get("query") or "").strip()
        mode = args.get("mode") or "all"
        backend_wish = args.get("backend") or None
        try:
            limit = int(args.get("limit") or self._config.get("search_top_k") or 8)
        except (ValueError, TypeError):
            return json.dumps({"error": "limit must be an integer (1-32)",
                               "hint": "omitting it uses search_top_k from the config",
                               "rack_status": self._rack_status_slot(
                                   self._cards_now(), skipped="rejected: limit must be an integer (1-32)")},
                              ensure_ascii=False)
        limit = max(1, min(limit, 32))
        cards = self._cards_now()   # rebuilt on call — routing and answer share one source
        if not query:
            return json.dumps({"error": "query required",
                               "hint": "What are you looking for? One keyword is enough, e.g. 'Relay Port'",
                               "rack_status": self._rack_status_slot(cards, skipped="rejected: query required")},
                              ensure_ascii=False)

        chosen = list(self._rack.values())
        if backend_wish:
            if backend_wish not in self._rack:
                return json.dumps({
                    "error": "unknown backend %r" % backend_wish,
                    "hint": "registered: %s" % ", ".join(sorted(self._rack)),
                    "rack_status": self._rack_status_slot(
                        cards, skipped="not requested (backend %r unknown)" % backend_wish),
                }, ensure_ascii=False)
            chosen = [self._rack[backend_wish]]

        requested = list(_SEARCH_MODES) if mode == "all" else [mode]
        if mode != "all" and mode not in _SEARCH_MODES:
            return json.dumps({"error": "unknown mode %r" % mode,
                               "hint": "modes: all|fts|vec|pointers",
                               "rack_status": self._rack_status_slot(
                                   cards, skipped="rejected: unknown mode %r" % mode)},
                              ensure_ascii=False)

        runs, engine_errors, vec_backlog = [], [], 0
        ran: dict = {}           # backend -> modes whose fan-out ran (hit or zero)
        skipped: dict = {}       # backend -> short reason (mode not carried)
        carried = sorted({m for b in chosen for m in cards[b.name]["modes"]})
        if mode != "all" and mode not in carried:
            # the degradation law Degradation: a specifically requested mode that NO backend
            # in the rack carries is an honest error — never a silent zero-result.
            return json.dumps({
                "error": "no backend carries mode %r" % mode,
                "hint": "available modes: %s" % (", ".join(carried) or "none"),
                "rack_status": self._rack_status_slot(
                    cards, skipped="requested mode %r not carried" % mode),
            }, ensure_ascii=False)
        for backend in chosen:
            modes = [m for m in requested if m in tuple(cards[backend.name]["modes"])]
            if not modes:
                if backend_wish:
                    return json.dumps({
                        "error": "backend %r does not carry mode %r" % (backend.name, mode),
                        "hint": "available modes: %s" % ", ".join(
                            cards[backend.name]["modes"] or ["none"]),
                        "rack_status": self._rack_status_slot(
                            cards, ran, {backend.name: "requested mode %r not carried" % mode}),
                    }, ensure_ascii=False)
                skipped[backend.name] = ("mode %r not carried" % mode if mode != "all"
                                         else "no live search modes")
                continue
            for m in modes:
                try:
                    hits = backend.search(query, m, limit) or []
                    for h in hits:
                        h["mode"] = m
                    # an engine's completeness note (vec backfill batch) — popped, never fused
                    backlog = max((int(h.pop("_vec_backlog", 0) or 0) for h in hits), default=0)
                    vec_backlog = max(vec_backlog, backlog)
                    runs.append(hits)
                    ran.setdefault(backend.name, []).append(m)
                except Exception as exc:
                    engine_errors.append({"backend": backend.name, "mode": m,
                                          "error": "%s: %s" % (type(exc).__name__, exc)})

        merged = self._rrf_merge(runs, limit)
        ms = int((time.time() - started) * 1000)
        self._touch_facts(merged)
        result = {"query": query, "mode": mode, "backend": backend_wish,
                  "ms": ms, "results": merged, "engine_errors": engine_errors,
                  "rack_status": self._rack_status_slot(
                      cards, ran, skipped, engine_errors,
                      reason=("only %r was targeted" % backend_wish) if backend_wish else None)}
        if not merged and not engine_errors:
            hint = "0 results."
            if backend_wish:
                hint += " Only %r was searched — try backend=None for the whole rack." % backend_wish
            hint += " Try mode 'vec' (paraphrases) or 'pointers' (directory);"
            hint += " broader phrasing helps too."
            result["hint"] = hint
        if vec_backlog:   # honest incompleteness note: newer facts may sit un-embedded
            result["vec_backlog"] = vec_backlog
        # the cards-on-delta law: the card prose leaves the provider ONLY on a
        # membership delta (the first tool answer of this instance counts as
        # one — the memo starts empty) or on the first answer itself; an
        # unchanged stand answers with the compact marker (the rack_status
        # slot above stays on EVERY answer, unchanged).
        stand_now = self._membership_stand_hash()
        if getattr(self, "_cards_served_stand", None) != stand_now:
            if cards:   # additive in-flow slot: the rack cards, rebuilt fresh
                result["cards"] = cards
            self._remember_answered_stand()
        else:
            result["cards"] = {
                "cards_unchanged": True,
                "hint": "memrack_status for the card prose",
                "write_targets": {
                    n: bool(callable(getattr(b, "write", None)))
                    for n, b in sorted(self._rack.items())},
            }
        return json.dumps(result, indent=2, ensure_ascii=False)

    @staticmethod
    def _rrf_merge(runs, limit):
        """RRF 60 + collapse per target + deterministic tiebreak — the fusion law."""
        pool: dict = {}
        for hits in runs:
            for hit in hits:
                target = hit.get("target")
                if target is None or str(target).strip() == "":
                    # the fallback key keeps the BACKEND identity: identical
                    # content out of DIFFERENT backends must not collapse
                    # into one pool entry (the printf-pitfall law: branch
                    # first, then format — never feed a raw None into %s)
                    key = "%s:content:%s" % (str(hit.get("backend") or ""),
                                             hit.get("content") or "")
                else:
                    key = "%s:%s" % (hit.get("backend"), target)
                entry = pool.setdefault(key, {
                    "backend": hit.get("backend"), "kind": hit.get("kind"), "id": hit.get("id"),
                    "content": hit.get("content"), "target": target,
                    "snippet": hit.get("snippet"),
                    "chunk_id": hit.get("chunk_id"),
                    "source_ref": hit.get("source_ref"),
                    "ref": hit.get("ref"),
                    "rank": hit.get("rank"),
                    "distance": hit.get("distance"),
                    "ts": hit.get("ts"), "hits": hit.get("hits") or 0,
                    "score": 0.0, "found_by": []})
                entry["score"] += 1.0 / (60.0 + max(1, int(hit.get("rank") or 99)))
                entry["found_by"].append(hit.get("mode") or hit.get("kind"))
        merged = list(pool.values())
        merged.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)   # Phase 1: ts DESC
        merged.sort(key=lambda e: (-e["score"], -(e.get("hits") or 0)))    # Phase 2 (stable): score, heat DESC
        merged = merged[:limit]
        for e in merged:
            e["found_by"] = sorted({m for m in e["found_by"] if m})
        return merged

    def _touch_facts(self, merged):
        """hits++ on returned rows, grouped per backend (Heat = report basis).
        base-db facts burn LEDGER heat; vec doc hits burn the backend's OWN
        per-chunk counter (kind gate: fact|doc)."""
        by_backend: dict = {}
        for hit in merged:
            if hit.get("kind") not in ("fact", "doc"):
                continue
            tid = hit.get("id")
            if tid is None:
                continue
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


    # ── legacy import job — prerequisite of the profile migration ──
    def _tool_vec_add(self, args):
        """T-047: ingest ONE source into the vec store's OWN corpus (documents
        as chunks — no ledger harvest anymore). Honest error when the vec
        backend is not in the rack; the engine's own fail-loud (pypdf missing,
        empty extraction, embedder gap) answers with a usable hint."""
        args = dict(args or {})
        engine = self._rack.get("vec")
        if engine is None or not hasattr(engine, "add_source"):
            return {"error": "the vec backend is not in the rack (toggle "
                             "'vec_in_rack' on in the memrack config)",
                    "hint": "hermes memrack status shows the cards"}
        payload = {}
        for key in ("source_ref", "text", "path", "chunk_chars",
                    "chunk_overlap", "pages"):
            val = args.get(key)
            if val not in (None, ""):
                payload[key] = val
        if "source_ref" not in payload:
            return {"error": "source_ref required (a stable reading reference, "
                             "e.g. the wiki path or file path of the document)",
                    "hint": "text OR path carries the document itself"}
        if ("text" in payload) == ("path" in payload):
            return {"error": "exactly ONE of text/path is required",
                    "hint": "pass the document as text or as a file path"}
        try:
            out = engine.add_source(**payload)
            self._remember_answered_stand()   # memo hygiene: the next search stays consistent
            return out
        except Exception as exc:
            return {"error": "vec ingest failed: %s: %s" % (type(exc).__name__, exc),
                    "source_ref": payload.get("source_ref")}

    def _tool_import(self, args):
        """Reads builtin-memory files (MEMORY.md/USER.md, §-delimited) and imports
        each entry as a fact row: hash-dedupe = idempotent (running it twice
        does no harm), target='builtin:<file>'. ONE job per profile, no cron."""
        source = (args.get("source") or "both")
        if source not in ("memory", "user", "both"):
            return json.dumps({"error": "source: memory|user|both"}, ensure_ascii=False)
        mem_dir = Path(self._hermes_home) / "memories"
        files = (
            [("MEMORY.md", "builtin:MEMORY.md")] if source in ("memory", "both") else []
        ) + ([("USER.md", "builtin:USER.md")] if source in ("user", "both") else [])
        backend = self._routed_for_kind(EXPLICIT)
        if backend is None:
            return json.dumps({"error": "no explicit-floor backend in the rack"}, ensure_ascii=False)
        if not callable(getattr(backend, "write", None)):
            return json.dumps({"error": "backend %r does not carry write" % backend.name},
                              ensure_ascii=False)
        report = {"imported": 0, "duplicates": 0, "files": [], "errors": []}
        accepts_hint = _write_accepts_target_hint(backend.write)
        if not accepts_hint:
            # a foreign backend ignoring target_hint: DEGRADE (log + proceed), never a
            # TypeError per entry — honestly flagged, RRF collapse risk named
            _LOG.warning("memrack import: backend %r ignores target_hint — importing "
                         "without per-entry targets", backend.name)
            report["note"] = ("backend %r ignores target_hint — entries imported without "
                              "per-entry targets; same-content rows may collapse under "
                              "the RRF merge" % backend.name)
        for fname, prefix in files:
            path = mem_dir / fname
            if not path.exists():
                report["files"].append({"file": fname, "entries": 0, "imported": 0,
                                        "duplicates": 0, "note": "not present"})
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            # CRLF-safe §-split (Windows-edited memory files split, too)
            entries = [e.strip() for e in re.split(r"\r?\n\u00a7\r?\n", raw)
                       if len(e.strip()) >= 10]
            file_imported = file_dup = 0
            for entry in entries:
                try:
                    # S-3-HARDENING: the target identifies THE ENTRY, NOT the file —
                    # the RRF merge collapses per target: a file-wide target would
                    # silently swallow N-1 hits.
                    kwargs = {"tags": ["import:" + fname]}
                    if accepts_hint:
                        tag_hash = hashlib.sha256(entry.encode("utf-8")).hexdigest()[:8]
                        kwargs["target_hint"] = "%s#%s" % (prefix, tag_hash)
                    out = backend.write(EXPLICIT, entry, **kwargs)
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

    # ── write: agent chooses the backend (the routing law) ──
    def _tool_write(self, args):
        content = str(args.get("content") or "").strip()
        kind = args.get("kind") or EXPLICIT
        if kind == "curation":        # legacy vocabulary — tolerated, stored as curated (A-3)
            kind = CURATED
        backend_wish = args.get("backend") or None
        tags = args.get("tags") if isinstance(args.get("tags"), list) else None
        if not content:
            return json.dumps({"error": "content required"})
        if kind not in WRITE_KINDS:
            return json.dumps({"error": "kind %r is not a write kind" % kind,
                               "hint": "kinds: explicit|curated — episodic is Hermes-native, "
                                       "semantic is a SEARCH MODE (vec), not a storage destination."},
                              ensure_ascii=False)
        if backend_wish and backend_wish not in self._rack:
            return json.dumps({"error": "unknown backend %r" % backend_wish,
                               "hint": "registered: %s" % ", ".join(sorted(self._rack))},
                              ensure_ascii=False)
        backend = self._rack.get(backend_wish) if backend_wish else \
            self._routed_for_kind(kind)   # no choice = deterministic source rule
        if backend is None:
            return json.dumps({"error": "no backend for kind %r in the rack" % kind,
                               "hint": "memrack_status shows the cards"})
        write_fn = getattr(backend, "write", None)
        if not callable(write_fn):
            return json.dumps({
                "error": "backend %r does not carry an atomic write" % backend.name,
                "hint": "structural knowledge belongs as a wiki page (file tools); "
                        "atomic facts: memrack_write without the backend param.",
            }, ensure_ascii=False)
        try:
            result = write_fn(kind, content, tags=tags, session_id=self._session_id)
            assert isinstance(result, dict), "backend write must report a dict"
        except Exception as exc:
            return json.dumps({"error": "write failed: %s: %s" % (type(exc).__name__, exc)},
                              ensure_ascii=False)
        self._log_route("memrack_write", kind, backend.name)
        result = dict(result)
        cards = self._cards_now()   # additive in-flow slot: the rack cards, rebuilt fresh
        if cards:
            result["cards"] = cards
        self._remember_answered_stand()   # the write's own prose counts — the next search is consistent
        return json.dumps(result, indent=2, ensure_ascii=False)

    def _rescan_dock(self) -> list:
        """Cheap fs-only rescan: manifests on disk that this session's rack has not loaded —
        they live 'registered (next session)' (bridge-watch spirit, the docking law).
        NOT pending: manifests that FAILED to load (a broken dock is already reported
        under docking errors — its error entry keys on the manifest `file`, not the
        backend name, T-029 bug 3) and manifests already loaded even when their file
        stem differs from the backend's name."""
        dock = docked_dir(self._hermes_home)
        if not (dock and dock.is_dir()):
            return []
        known = set(self._rack) | {p.get("name") for p in self._parked}
        for b in self._rack.values():
            mf = getattr(b, "manifest_file", None)
            if mf:
                known.add(Path(mf).stem)          # the manifest THIS backend was loaded from
        for e in self._load_errors:
            f = e.get("file")
            if f:
                known.add(Path(f).stem)           # a failed manifest = honest error, never pending
        pending = []
        for path in sorted(list(dock.glob("*.yaml")) + list(dock.glob("*.yml"))):
            if path.name.startswith("_") or path.name.startswith("disabled-"):
                continue
            # the containment law on the rescan's path assembly: a manifest
            # (or symlink at a manifest name) that resolves OUTSIDE the dock
            # directory is an honest refusal, never a pending promise
            try:
                contained = path.resolve().parent == dock.resolve()
            except OSError:
                contained = False
            if not contained:
                pending.append({
                    "manifest": path.name,
                    "note": "refused: resolves outside the dock directory "
                            "(containment — never registered)"})
                continue
            if path.stem not in known:
                pending.append({"manifest": path.name, "note": "registered (next session)"})
        return pending

    # ── inventory ──
    def status(self) -> dict:
        cards = self._cards_now()   # rebuilt on call — never the init snapshot
        out: dict = {"plugin": PLUGIN_NAME, "version": __version__}
        if cards:
            out["cards"] = cards
        # T-036 Addendum (2): the last initialize's dock_want consumption —
        # the machine receipt or its honest refusal (surfacing choice made by
        # the caller here; absent on a fresh pre-init or a no-choice init).
        receipt = getattr(self, "_dock_want_outcome", None)
        if receipt is not None:
            out["dock_want_receipt"] = receipt
        # the one-shot receipt law (read-side): the consumed `dock_want`
        # rides only as `dock_want_last` {provider, outcome, at} — the
        # additive slot keeps the last outcome visible after the select
        # itself left both stores (an absent receipt stays absent).
        last = (self._config or {}).get("dock_want_last")
        if isinstance(last, dict) and last.get("provider"):
            out["dock_want_last"] = dict(last)
        out.update({
            "backends": sorted(self._rack),
            "gap_list": self._gaps,
            "parked": self._parked,
            "docking_errors": self._load_errors,
            "active_backend": self._active.name if self._active else None,
            "forced_backend": self._config.get("backend"),
            "enabled_builtins": self._config.get("enabled_backends", list(_DEFAULT_ENABLED)),
            "pending_docks": self._rescan_dock(),
            "session_id": self._session_id,
        })
        # The uninstall-grace surface (honest, never a silent absence):
        # docked-but-inactive cards whose provider install left the house —
        # with the flag, the kept-until note and the RETENTION countdown —
        # plus this initialize's removal receipts.
        if self._hermes_home:
            try:
                out["uninstalled_docks"] = self._docking_module() \
                    .uninstalled_cards(
                    self._hermes_home,
                    retention_days=self._config.get("dock_retention_days"))
            except Exception:
                pass    # the status never crashes over the surface (it degrades)
            report = getattr(self, "_uninstalled_report", None)
            if isinstance(report, dict):
                if report.get("removed"):
                    out["retention_cleanups"] = list(report["removed"])
                # the restore receipts (with the reactivate hint) and the
                # switch-consistency receipts (every store switch this
                # initialize flipped off) surface here too — honest, additive
                if report.get("restored"):
                    out["restored_docks"] = list(report["restored"])
                if report.get("switch_off"):
                    out["switch_consistency"] = list(report["switch_off"])
        return out


_TOOL_DESCRIPTIONS = {
    "memrack_search": "Search the memory-rack: hybrid fan-out across all backends and search "
                      "modes (default mode=all), or target one backend/mode. Returns hits with "
                      "target references (wiki path/L42 etc.) for whole-page follow-up reading.",
    "memrack_write": "Store one atomic fact (one sentence) in the memory-rack; the agent chooses "
                     "the destination backend via the rack card (default: base-db). Structural "
                     "knowledge belongs in wiki pages (file tools), not here.",
    "memrack_status": "Rack inventory: backends with card (best_for/limits/modes), heat counters, "
                      "gaps (engines that degraded), parked/uninstalled-docked cards (with the "
                      "retention countdown), retention removal receipts and docking errors; "
                      "restored-dock receipts carry the flip-the-switch-to-reactivate hint and "
                      "switch_consistency names every store switch the grace law flipped off.",
    "memrack_import": "Import the builtin memory's legacy entries (MEMORY.md/USER.md, §-delimited) "
                      "as fact rows — idempotent via hash-dedupe. Migration job, once per profile.",
    "memrack_vec_add": "Ingest ONE document into the vec store's OWN corpus (the autonomous "
                       "document store): text or a file path (.md/.txt/.pdf), mechanically chunked, "
                       "batched embedded, searchable in mode vec; re-ingest of a source_ref replaces "
                       "it. The corpus holds ONLY what was ingested here — not the fact ledger.",
    "memrack_dock": "The catalog-provider docking machine: list installed, inactive catalog memory "
                    "providers that could dock into the rack, show existing dock cards (with the "
                    "mirrored effective tool names), run the isolated probe (action=probe writes "
                    "the draft card), refresh after a provider update (drift-recheck), or remove "
                    "a dock. Listing/offering never docks anything — the user decides; writes go "
                    "through an explicit, decision-recorded card (fail-closed otherwise). An "
                    "install that left the house parks its card instead (the uninstall grace: "
                    "kept until the dock_retention_days retention — default 14 days — then "
                    "removed with a per-file backup; a re-install restores it).",
    "memrack_card_review": "Confirm one docked provider's PROPOSED card after the chat "
                           "agreement: sets the reviewed best_for/limits texts VERBATIM "
                           "(backend=<dock name>), clears the draft marker and records one "
                           "chat-confirm revision. An unknown backend or empty texts is an "
                           "honest error; a reviewed card is drift-recheck-immun.",
}


_HOOK_UNSET = object()
"""Sentinel for the parallel fan-out's per-backend slots: a slot still
holding it means THAT member never answered within its budget (no hook, a
loss, or the budget ran out) — the collection distinguishes all three."""


def _is_docked_member(backend) -> bool:
    """The membership test the config-surface family uses: docked = a card
    member (manifest data, or a backends.d code file), never the
    plugin-shipped built-ins."""
    return bool(getattr(backend, "manifest", None)) or \
        bool(getattr(backend, "docked_file", False))


def _session_end_payload(backend, args, kwargs):
    """The per-card transcript gate for the session-end fan-out: docked
    members receive the session-end EVENT, and the TRANSCRIPT only when
    their card records the human-confirmed ``forward_transcript`` decision
    (default off — human-authored, exactly like forward_writes /
    forward_turns; a card carrying no decision is fail-closed). Code-first
    built-ins keep today's fan-out byte-identical: the SAME args object
    rides through."""
    call_args = tuple(args)
    if not _is_docked_member(backend) or _truthy(
            (getattr(backend, "manifest", None) or {}).get("forward_transcript")):
        return call_args, kwargs
    return (None,) + call_args[1:], kwargs


_MEMRACK_MIRROR_SKIP_CONTEXTS = ("cron", "subagent")
"""The automatic contexts (the ABC's agent_context vocabulary) the memory
MIRROR path serves no writes to; explicit memrack_write tool calls stay the
agent's decision whatever the context."""


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
