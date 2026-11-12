"""Declarative config surface for the memrack desktop panel (gear → Plugins → memrack).

Loaded BY PATH (never imported as a package) and may import only
``plugins.memory.config_schema``... except the T-037 amendment: the
``CONFIG_SCHEMA`` below is BUILT AT IMPORT — the module body resolves the
plugin's OWN config-surface seams (``memrack/config_surface.py``, the T-036
helpers) by path with the provider core's ONE-COPY convention, so the
declared payload carries the dynamic ``<name>_in_rack`` toggle family and
the ``dock_want`` select beside the static five. Storage = ``flat_json`` —
the desktop PUT writes ``<hermes_home>/memrack/config.json`` (atomic 0600,
partial saves touch only submitted keys). The provider reads BOTH stores:
``config.yaml`` ``plugins.memrack`` first, flat_json keys win (the newest
user intent). The CLI wizard surface (``get_config_schema`` on the provider
class) carries the same family, instance-derived.

STALENESS (plan T-037): the web server caches this schema per file path,
in-process. The rendered family — built-in names, the ACTIVE docked cards,
the ``dock_want`` offers, the toggle defaults — is FROZEN at import time:
a backend docked AFTER this module ran appears (and re-offers) only after
a backend restart. The MEMBERSHIP ITSELF is not frozen: the persisted
``<name>_in_rack`` keys act per ``/new`` (the provider's initialize fold —
the same law the ``dock_want`` consumption follows); only the SURFACE
lags, never the rack.
"""

import importlib.util
import logging
import os
from pathlib import Path

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SELECT,
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
    STORAGE_FLAT_JSON,
)

_LOG = logging.getLogger("memrack.config_schema")

_PLUGIN_ROOT = Path(__file__).resolve().parent
_BUILTIN_NAMES = ("base-db", "state-episodic", "wiki", "vec")
"""Mirror of the provider core's ``_SHIPPED_BUILTINS`` (the candidate
code-first built-ins: rack members AND parked ones; the filename stem carries
the real backend name only in these three shipped cases, so the honest
pre-init scan candidate list is the mirrored constant, not a dir glob)."""


# ── the seam plumbing (spec-path, one copy per process — the provider
#    core's own conventions; each spec-load-independent module mirrors the
#    path computation, the parity is the established law) ────────────────────

def _config_surface_module():
    """The T-036 seams beside THIS file, under the ONE canonical
    sys.modules key the provider core uses (an already-loaded copy wins:
    one module per process, attribute patches stay visible)."""
    import sys
    cs = sys.modules.get("memrack_config_surface")
    if cs is not None:
        return cs
    spec = importlib.util.spec_from_file_location(
        "memrack_config_surface", _PLUGIN_ROOT / "memrack" / "config_surface.py")
    if spec is None or spec.loader is None:
        raise ImportError("config surface not resolvable beside the schema")
    cs = importlib.util.module_from_spec(spec)
    sys.modules["memrack_config_surface"] = cs
    spec.loader.exec_module(cs)
    return cs


def _home_from_file():
    """The install-home derivation from THIS file's location (the
    cross-profile law): a DEPLOYED copy lives at
    ``<home>/plugins/memrack/config_schema.py``, so
    ``Path(__file__).resolve().parents[2]`` is the home — accepted ONLY
    when the sibling ``<home>/plugins/memrack`` directory exists (the
    deployed-copy shape). A repo checkout resolves outside a plugin tree
    and returns None: every caller falls back to ``$HERMES_HOME``/the
    default home exactly as before (the suite's environment law)."""
    try:
        candidate = Path(__file__).resolve().parents[2]
    except (IndexError, OSError, ValueError):
        return None
    if (candidate / "plugins" / "memrack").is_dir():
        return candidate
    return None


def _hermes_home():
    """The provider's home, in priority order: the ``__file__``-derived
    install home (valid only for a DEPLOYED copy — wins over the
    process-global ``$HERMES_HOME`` so a shared process never resolves one
    profile's module against another profile's home), then ``$HERMES_HOME``,
    then the default home. The derived step is what keeps the declared
    gear pane's import-time scan inside the copy's OWN profile."""
    derived = _home_from_file()
    if derived is not None:
        return derived
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _docked_dir(home=None):
    """``$HERMES_HOME/plugins/memrack/backends.d`` — the docked-dir path
    mirror (identical computation, pinned by parity against the seams)."""
    if home:
        return Path(home) / "plugins" / "memrack" / "backends.d"
    return _hermes_home() / "plugins" / "memrack" / "backends.d"


def _profile_config(home):
    """The provider's merged config dict, mirrored from the core's
    ``_profile_config``: ``plugins.memrack`` from the profile config.yaml
    (yaml may be broken — it must not kill the desktop config), then the
    flat_json ``<home>/memrack/config.json`` whose keys WIN. Both reads
    fail soft here: a broken store degrades to the shipped defaults, the
    render stays."""
    merged = {}
    try:
        import yaml
        raw = yaml.safe_load((Path(home) / "config.yaml").read_text(
            encoding="utf-8")) or {}
        merged.update(dict((raw.get("plugins") or {}).get("memrack") or {}))
    except Exception:
        pass
    try:
        import json
        flat = Path(home) / "memrack" / "config.json"
        if flat.is_file():
            data = json.loads(flat.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                merged.update({k: v for k, v in data.items() if k})
    except Exception:
        pass
    return merged


def _active_docked_names(home):
    """Stems of the ACTIVE dock cards on file (presence = registered; parked
    ``disabled-`` cards reappear via their toggle/dock_want path, mirroring
    the provider instance's derivation: parked dock cards are NOT toggles).
    ``_``-stems stay out (the core's scan law)."""
    names = set()
    dock = _docked_dir(home)
    if not dock.is_dir():
        return names
    try:
        for path in sorted(dock.glob("*.yaml")) + sorted(dock.glob("*.yml")):
            if path.name.startswith("_") or path.name.startswith("disabled-"):
                continue
            if path.stem:
                names.add(path.stem)
    except OSError as exc:
        _LOG.warning("memrack dock scan unreadable (%s): %s", dock, exc)
    return names


def _offers():
    """The docking machine's CURRENT catalog offers — the machine is the
    offer source (its own filtering stays); hermes-less/in-broken-host
    processes get the honest empty list (the select still renders ``""``)."""
    try:
        docking = _docking_module()
        return docking.catalog_offers() or []
    except Exception as exc:
        _LOG.info("memrack dock offers unavailable here (%s: %s)",
                  type(exc).__name__, exc)
        return []


def _docking_module():
    """The docking machine beside THIS file under the canonical
    ``memrack_docking_core`` key (the ONE-copy rule both the provider core
    and the seams resolve through)."""
    import sys
    mod = sys.modules.get("memrack_docking_core")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        "memrack_docking_core", _PLUGIN_ROOT / "memrack" / "docking.py")
    if spec is None or spec.loader is None:
        raise ImportError("docking machine not resolvable beside the schema")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memrack_docking_core"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── the static five (the pane base fields, byte-identical values: the
#    former membership select LEFT the pane — the toggle family below is
#    the pane's ONE membership UI; the ``enabled_backends`` string stays
#    internal store repr that the initialize fold maintains) ────────────────

_STATIC_FIELDS = (
    ProviderField(
        key="backend", label="Force backend", kind=KIND_SELECT, default="",
        description="Force ONE backend for ALL routes (migration/transition override)",
        options=(
            ProviderFieldOption(value="", label="no force (route by kind)"),
            ProviderFieldOption(value="base-db", label="base-db"),
            ProviderFieldOption(value="wiki", label="wiki"),
        ),
        inline=True,
    ),
    ProviderField(
        key="auto_enable", label="Auto-enable at activation", kind=KIND_BOOL,
        default="false",
        description="Enable base-db + wiki automatically on activation (default off)",
        inline=True,
    ),
    ProviderField(
        key="search_top_k", label="Search top_k", kind=KIND_NUMBER, default="8",
        description="Default result count for memrack_search (max 32)",
        inline=True,
    ),
    ProviderField(
        key="embedding_model", label="Embedding model", kind=KIND_TEXT,
        default="nomic-embed-text", group="vec",
        description="Ollama embedding model for the optional vec engine",
    ),
    ProviderField(
        key="ollama_host", label="Ollama host", kind=KIND_TEXT,
        default="http://127.0.0.1:11434", group="vec",
        description="Ollama endpoint for vec embeddings",
    ),
    ProviderField(
        key="vec_ingest_roots", label="Vec ingest roots", kind=KIND_TEXT,
        default="", group="vec",
        description="Comma-separated path roots the vec engine may ingest "
                    "files from (fallback: the home dir + wiki_root); "
                    "HERMES_HOME, ~/.ssh and .env files always refuse",
    ),
    ProviderField(
        key="dock_retention_days", label="Uninstall retention days",
        kind=KIND_NUMBER, default="14",
        description="Days an uninstalled dock card stays parked before card + "
                    "shim are removed (0 = immediately, -1 = never)",
        inline=True,
    ),
    ProviderField(
        key="fanout_timeout_s", label="Fan-out timeout (s)",
        kind=KIND_NUMBER, default="5",
        description="Per-backend hook fan-out timeout in seconds (default 5): "
                    "a hanging backend becomes an honest gap after this "
                    "budget, the rest of the rack still delivers",
        inline=True,
    ),
    ProviderField(
        key="prefetch_max_chars", label="Prefetch budget (chars)",
        kind=KIND_NUMBER, default="4000",
        description="Total character budget for the merged prefetch block "
                    "(default 4000): longer merges truncate with an honest "
                    "'+N chars truncated' note",
        inline=True,
    ),
)


# ── the dynamic family (the T-036 seams' output, shaped for the renderer) ───

def _toggle_field(entry):
    """One wizard-shaped seam toggle → the declared bool field (the
    ``auto_enable`` kind precedent: a bool renders the switch; the
    choices pair stays the kind's convention, no options tuple)."""
    return ProviderField(
        key=entry["key"],
        label=entry.get("label") or entry["key"],
        kind=KIND_BOOL,
        default=entry.get("default", "false"),
        description=entry.get("description", ""),
        inline=True,
    )


def _dock_want_field():
    """The dock_want select (Addendum (2)): choices = the machine's offers
    plus the none choice — ordered, self-excluded by the machine."""
    entry = _config_surface_module().dock_want_field(_offers())
    choices = list(entry.get("choices") or [""])
    if "" not in choices:
        choices.insert(0, "")
    return ProviderField(
        key=entry["key"],
        label=entry.get("label") or entry["key"],
        kind=KIND_SELECT,
        default=entry.get("default", ""),
        description=entry.get("description", ""),
        options=tuple(
            ProviderFieldOption(
                value=choice,
                label="none (no auto-dock)" if choice == "" else choice)
            for choice in choices),
        inline=True,
    )


def _dynamic_fields():
    """The appended family at import time: per-backend toggles plus the
    dock_want select, each block failing INDEPENDENTLY (the core's
    per-builder convention) — a failing block degrades to the surviving
    static surface, the setup never crashes, the Basisfall keeps its
    exact shape."""
    fields = []
    try:
        cs = _config_surface_module()
        home = _hermes_home()
        config = _profile_config(home)
        fields += [_toggle_field(entry) for entry in cs.membership_fields(
            config, _BUILTIN_NAMES, sorted(_active_docked_names(home)))]
    except Exception as exc:
        _LOG.warning("memrack declared toggle family degraded: %s: %s",
                     type(exc).__name__, exc)
    try:
        fields.append(_dock_want_field())
    except Exception as exc:
        _LOG.warning("memrack declared dock_want degraded: %s: %s",
                     type(exc).__name__, exc)
    return tuple(fields)


CONFIG_SCHEMA = ProviderConfigSchema(
    name="memrack",
    label="memrack",
    storage=STORAGE_FLAT_JSON,
    fields=_STATIC_FIELDS + _dynamic_fields(),
)
