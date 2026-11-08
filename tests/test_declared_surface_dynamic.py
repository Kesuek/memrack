"""T-037 contract tests — the DECLARED desktop surface (config_schema.py)
carries the dynamic membership-toggle family itself.

Plan T-037: the gear pane (desktop /config?surface=declared) kept rendering
rack membership as a dropdown because the core builds the declared payload
ONLY from the static ``provider.fields`` list of the by-path-loaded
``CONFIG_SCHEMA``. The fix is PLUGIN-SIDE ONLY: ``config_schema.py`` builds
``CONFIG_SCHEMA`` at import time — the six static fields stay first and
byte-identical, then the config-surface seams (T-036) append the
``<name>_in_rack`` toggle family (built-ins + ACTIVE docked cards from
``backends.d``) plus the ``dock_want`` select. Zero core changes (the core's
loader reads ``module.CONFIG_SCHEMA``, so a dynamic module-level value is
enough).

The acting law (plan T-037 staleness note: 'der Membership-Wert selbst
wirkt per /new (initialize-Fold)': the persisted raw toggle keys the gear
pane writes fold at the provider's initialize() through the EXISTING
T-036 fold (``_fold_membership_toggles``); docked presence is the state,
the stores stay byte-identical under re-initialization).

Ground rules: no network, no host plugin registry — the schema module
under test is executed by PATH (exactly how the core loads it); scratch
homes via ``monkeypatch.setenv("HERMES_HOME", ...)``; the offer list may
carry the Hermes tree's bundled providers, so ``dock_want`` assertions stay
tolerant (empty choice first, self excluded), while the membership family
assertions are exact.
"""
import importlib.util
import json
import pathlib
import re
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parent.parent

_HERMES_TREE = pathlib.Path.home() / ".hermes" / "hermes-agent"
if str(_HERMES_TREE) not in sys.path:
    sys.path.insert(0, str(_HERMES_TREE))

# config_schema.py imports the runtime surface `plugins.memory.config_schema`
# (→ hermes_cli.config → hermes_yaml): without the Hermes tree those imports
# are unsatisfiable and the declared-schema tests cannot run. Skip at module
# top instead of failing (the established bridge/wizard tests elsewhere stay
# runnable everywhere).
pytest.importorskip(
    "plugins.memory.config_schema",
    reason="declared-surface tests need the Hermes runtime "
           "(plugins.memory.config_schema); fresh checkouts without the "
           "Hermes tree skip instead of failing")

_SCHEMA_PATH = _ROOT / "config_schema.py"
_STATIC_KEYS = ("backend", "auto_enable", "search_top_k",
                "embedding_model", "ollama_host")


def _exec_schema(module_name):
    """Load config_schema.py BY PATH — exactly the core's documented loader
    shape (spec_from_file_location + exec_module, no package import)."""
    spec = importlib.util.spec_from_file_location(module_name, _SCHEMA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_schema(tmp_path, monkeypatch, *, module_name, config_json=None,
                 config_yaml=None, dock_cards=()):
    """Build a scratch HERMES_HOME (flat json, yaml block, dock cards) and
    exec the schema module fresh — CONFIG_SCHEMA is import-computed."""
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    if config_json is not None:
        flat = home / "memrack"
        flat.mkdir(parents=True, exist_ok=True)
        (flat / "config.json").write_text(
            json.dumps(config_json), encoding="utf-8")
    if config_yaml is not None:
        (home / "config.yaml").write_text(yaml.safe_dump(config_yaml),
                                          encoding="utf-8")
    if dock_cards:
        dock = home / "plugins" / "memrack" / "backends.d"
        dock.mkdir(parents=True, exist_ok=True)
        for name in dock_cards:
            card = {"name": name, "module": name + ".py",
                    "best_for": "PLACEHOLDER —", "limits": "PLACEHOLDER —",
                    "draft": True}
            (dock / (name + ".yaml")).write_text(
                yaml.safe_dump(card, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return _exec_schema(module_name)


def _fields_by_key(schema):
    return {f.key: f for f in schema.fields}


def _toggle_keys(schema):
    return [f.key for f in schema.fields if f.key.endswith("_in_rack")]


# ── (a) CONFIG_SCHEMA at import carries the live toggle family ──────────────

def test_declared_schema_carries_live_membership_toggles(tmp_path, monkeypatch):
    """The declared pane: CONFIG_SCHEMA is built at import — a tmp home with
    a dock card sim + a live flat config renders ONE bool toggle per backend
    (built-ins sized to the current stand, docked presence wins) appended
    AFTER the unchanged static five (the pane's ``enabled_backends`` select
    already left: the toggles are its only membership UI)."""
    mod = _load_schema(
        tmp_path, monkeypatch, module_name="_t037_live_family",
        config_json={"enabled_backends": "base-db,state-episodic"},
        dock_cards=["limbic"])
    by = _fields_by_key(mod.CONFIG_SCHEMA)
    import dataclasses

    # the family appended additively: toggles + dock_want, static order kept
    assert _STATIC_KEYS == tuple(f.key for f in mod.CONFIG_SCHEMA.fields[:5])
    for name, expect in (("base-db", "true"), ("limbic", "true"),
                         ("state-episodic", "true"), ("wiki", "false")):
        field = by["%s_in_rack" % name]
        assert field.kind == "bool", field
        assert field.default == expect, (name, field)
        assert field.inline is True, field
        assert dataclasses.asdict(field)["description"], field
    # restart-stable order law reused from the seams (sorted by name)
    assert _toggle_keys(mod.CONFIG_SCHEMA) == [
        "base-db_in_rack", "limbic_in_rack", "state-episodic_in_rack",
        "wiki_in_rack"], _toggle_keys(mod.CONFIG_SCHEMA)
    # the dock_want select with the none choice first (offers may include the
    # Hermes tree's bundled providers — tolerant against environment)
    dw = by["dock_want"]
    assert dw.kind == "select", dw
    assert dw.inline is True, dw
    values = [o.value for o in dw.options]
    assert values[0] == "", dw
    assert "memrack" not in values, dw


def test_declared_schema_without_docks_is_builtins_only(tmp_path, monkeypatch):
    """Plan T-037 (1): without any docked card the family is exactly the
    shipped built-ins (base-db floor ON, wiki opt-in OFF) + the dock_want
    select; the docked-dir path law mirrors the seams exactly."""
    mod = _load_schema(tmp_path, monkeypatch, module_name="_t037_no_docks",
                       config_json={})
    keys = _toggle_keys(mod.CONFIG_SCHEMA)
    assert keys == ["base-db_in_rack", "state-episodic_in_rack",
                    "wiki_in_rack"], keys
    by = _fields_by_key(mod.CONFIG_SCHEMA)
    assert by["state-episodic_in_rack"].default == "true", by
    assert by["wiki_in_rack"].default == "false", by
    # the path parity law: the module's dock dir is the SAME path the seams
    # resolve (config_surface._docked_dir)
    cs = sys.modules["memrack_config_surface"]
    assert str(mod._docked_dir(str(tmp_path / "hermes-home"))) == \
        str(cs._docked_dir(str(tmp_path / "hermes-home")))


# ── (b) the static five stay byte-identical at the top ───────────────────────

_BASE_STATIC = [
    {
        "key": "backend", "label": "Force backend", "kind": "select",
        "default": "",
        "description":
            "Force ONE backend for ALL routes (migration/transition override)",
        "placeholder": "",
        "options": (
            {"value": "", "label": "no force (route by kind)",
             "description": ""},
            {"value": "base-db", "label": "base-db", "description": ""},
            {"value": "wiki", "label": "wiki", "description": ""}),
        "env_key": None, "aliases": (), "env_fallbacks": (),
        "inline": True, "group": "", "info": "", "scope": "host",
    },
    {
        "key": "auto_enable", "label": "Auto-enable at activation",
        "kind": "bool", "default": "false",
        "description":
            "Enable base-db + wiki automatically on activation (default off)",
        "placeholder": "", "options": (), "env_key": None,
        "aliases": (), "env_fallbacks": (), "inline": True, "group": "",
        "info": "", "scope": "host",
    },
    {
        "key": "search_top_k", "label": "Search top_k", "kind": "number",
        "default": "8",
        "description": "Default result count for memrack_search (max 32)",
        "placeholder": "", "options": (), "env_key": None,
        "aliases": (), "env_fallbacks": (), "inline": True, "group": "",
        "info": "", "scope": "host",
    },
    {
        "key": "embedding_model", "label": "Embedding model", "kind": "text",
        "default": "nomic-embed-text",
        "description": "Ollama embedding model for the optional vec engine",
        "placeholder": "", "options": (), "env_key": None,
        "aliases": (), "env_fallbacks": (), "inline": False, "group": "vec",
        "info": "", "scope": "host",
    },
    {
        "key": "ollama_host", "label": "Ollama host", "kind": "text",
        "default": "http://127.0.0.1:11434",
        "description": "Ollama endpoint for vec embeddings",
        "placeholder": "", "options": (), "env_key": None,
        "aliases": (), "env_fallbacks": (), "inline": False, "group": "vec",
        "info": "", "scope": "host",
    },
]


def test_declared_static_five_fields_are_byte_identical(tmp_path, monkeypatch):
    """The five surviving base fields (backend, auto_enable, search_top_k,
    embedding/ollama group) stay UNCHANGED at the top of the pane — the
    family is appended ADDITIVELY and the ``enabled_backends`` select LEFT
    the pane (the toggle family is its only membership UI; the string
    stays internal store repr). Every dataclass value of the first five
    fields matches the pre-drop schema byte for byte."""
    mod = _load_schema(tmp_path, monkeypatch, module_name="_t037_static",
                       config_json={})
    import dataclasses
    first_five = [dataclasses.asdict(f) for f in mod.CONFIG_SCHEMA.fields[:5]]
    assert first_five == _BASE_STATIC, first_five
    assert _STATIC_KEYS == tuple(f.key for f in mod.CONFIG_SCHEMA.fields[:5])


# ── (c) the key law: no special characters, bool kind ───────────────────────

def test_declared_toggle_keys_are_clean_and_bool_kinded(tmp_path, monkeypatch):
    """Plan T-037 (4): 'keys ohne Sonderzeichen (Gesetz aus T-036),
    kind=bool' — every toggle key is <name>_in_rack over a slug name
    (letters/digits/dash/underscore only) and every family member that is
    not the select renders the bool kind."""
    mod = _load_schema(tmp_path, monkeypatch, module_name="_t037_keylaw",
                       config_json={"enabled_backends": "base-db,wiki"},
                       dock_cards=["wiki-memory", "limbic"])
    toggles = [f for f in mod.CONFIG_SCHEMA.fields
               if f.key.endswith("_in_rack")]
    assert toggles, "the family must exist"
    for field in toggles:
        assert field.kind == "bool", field
        stem = field.key[: -len("_in_rack")]
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", stem), field
        assert field.key == stem + "_in_rack", field
        assert field.default in ("true", "false"), field
    # every name that exists in the scan renders exactly once (deduped)
    keys = [f.key for f in toggles]
    assert len(keys) == len(set(keys)), keys
    by = _fields_by_key(mod.CONFIG_SCHEMA)
    assert by["wiki_in_rack"].default == "true", by          # stand governs
    assert by["wiki-memory_in_rack"].default == "true", by   # docked presence
    assert by["limbic_in_rack"].default == "true", by        # docked presence


def test_declared_toggle_default_follows_both_stores(tmp_path, monkeypatch):
    """The dual-store bridge law carried into the schema: the flat_json
    keys win (freshest user intent) — a yaml LIST stand must not beat a
    flat json stand, and its key-less shape must still render bools."""
    mod = _load_schema(
        tmp_path, monkeypatch, module_name="_t037_stores",
        config_json={"enabled_backends": "base-db"},
        config_yaml={"plugins": {"memrack": {
            "enabled_backends": ["base-db", "wiki"]}}})
    by = _fields_by_key(mod.CONFIG_SCHEMA)
    assert by["wiki_in_rack"].default == "false", by      # the json stand wins
    assert by["state-episodic_in_rack"].default == "false", by
    assert by["base-db_in_rack"].default == "true", by


# ── (d) the seams stay ONE-copy and degrade honestly ─────────────────────────

def test_declared_schema_degrades_honestly_and_shares_seams(
        tmp_path, monkeypatch):
    """The seam reuse law (the provider core's one-copy convention): both
    schema execs share the SAME config-surface module object under the
    canonical sys.modules key, so seam attribute patches stay visible — and
    a failing dynamic block degrades to the surviving static surface (the
    setup never crashes, the Basisfall keeps its exact shape)."""
    mod1 = _load_schema(tmp_path, monkeypatch, module_name="_t037_one_copy_1",
                        config_json={})
    cs = sys.modules.get("memrack_config_surface")
    assert cs is not None, "the schema must resolve the canonical seam key"
    assert mod1._config_surface_module() is cs

    # a patched seam stays VISIBLE to a second exec (one copy, visible patch)
    def _boom(_offers):
        raise RuntimeError("patched to fail")
    monkeypatch.setattr(cs, "dock_want_field", _boom)
    mod2 = _load_schema(tmp_path, monkeypatch,
                        module_name="_t037_one_copy_2", config_json={})
    assert sys.modules["memrack_config_surface"] is cs
    keys2 = {f.key for f in mod2.CONFIG_SCHEMA.fields}
    assert "dock_want" not in keys2, keys2        # the failed block degrades
    assert set(_STATIC_KEYS) <= keys2, keys2      # the static six stay
    assert _toggle_keys(mod2.CONFIG_SCHEMA), "the toggle block still renders"


# ── (e) the acting law: the persisted toggles act per /new ───────────────────

def _spec_provider():
    spec = importlib.util.spec_from_file_location("memrack_t037_dyn",
                                                  _ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _flat_write(tmp_path, data):
    home = tmp_path / "hermes-home"
    flat = home / "memrack"
    flat.mkdir(parents=True, exist_ok=True)
    (flat / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return home


def _card(tmp_path, name, *, parked=False):
    dock = tmp_path / "hermes-home" / "plugins" / "memrack" / "backends.d"
    dock.mkdir(parents=True, exist_ok=True)
    prefix = "disabled-" if parked else ""
    card = {"name": name, "module": name + ".py",
            "best_for": "PLACEHOLDER —", "limits": "PLACEHOLDER —",
            "draft": True}
    (dock / (prefix + name + ".yaml")).write_text(
        yaml.safe_dump(card, allow_unicode=True), encoding="utf-8")


def test_gear_toggle_acts_per_initialize(tmp_path):
    """Plan T-037 §5: 'der Membership-Wert selbst wirkt per /new (initialize-
    Fold)' — a persisted raw `wiki_in_rack` toggle (the gear pane's flat
    write) folds at initialize() through the EXISTING T-036 fold onto the
    current stand: the rack stand gains the member IN-MEMORY, the store is
    not touched (no hidden writes), and the instance schema renders the
    folded stand."""
    home = _flat_write(tmp_path, {"wiki_in_rack": "true"})
    mr = _spec_provider()
    prov = mr._MemrackBase()
    prov.initialize(session_id="T037G", hermes_home=str(home))
    folded = mr._norm_backends(prov._config.get("enabled_backends"))
    assert "wiki" in folded, prov._config
    assert "base-db" in folded and "state-episodic" in folded, prov._config
    # no store churn: the fold reads, it never rewrites the stores
    assert json.loads((home / "memrack" / "config.json").read_text(
        encoding="utf-8")) == {"wiki_in_rack": "true"}
    assert not (home / "config.yaml").exists()
    # the instance schema renders from the folded stand
    fields = {f["key"]: f for f in prov.get_config_schema()}
    assert mr._truthy(fields["wiki_in_rack"]["default"]) is True, fields


def test_gear_docked_toggle_parks_and_restores_per_initialize(tmp_path):
    """The docked half of the acting law: the persisted `<name>_in_rack`
    key for a docked card acts through the T-036 presence rename — OFF
    parks the card (the key is dropped, presence is the state), ON restores
    a parked card; the shim stays untouched."""
    home = _flat_write(tmp_path, {"fakesvc_in_rack": "false"})
    _card(tmp_path, "fakesvc")
    mr = _spec_provider()
    prov = mr._MemrackBase()
    prov.initialize(session_id="T037P", hermes_home=str(home))
    dock = home / "plugins" / "memrack" / "backends.d"
    assert (dock / "disabled-fakesvc.yaml").exists(), sorted(p.name for p in dock.iterdir())
    assert not (dock / "fakesvc.yaml").exists(), sorted(p.name for p in dock.iterdir())
    assert "fakesvc_in_rack" not in prov._config, prov._config

    # toggling back ON restores the card (the parked file returns) — the
    # gear pane's PUT writes the fresh "true" into the flat store first
    (home / "memrack" / "config.json").write_text(
        json.dumps({"fakesvc_in_rack": "true"}), encoding="utf-8")
    restored = mr._MemrackBase()
    restored.initialize(session_id="T037R", hermes_home=str(home))
    assert (dock / "fakesvc.yaml").exists(), sorted(p.name for p in dock.iterdir())
    assert not (dock / "disabled-fakesvc.yaml").exists(), sorted(p.name for p in dock.iterdir())


def test_gear_toggle_action_is_churn_free_across_inits(tmp_path):
    """The acting law must be IDEMPOTENT and churn-free: a second
    initialize over the same stores re-folds to the SAME stand and leaves
    both stores byte-identical (no hidden rewrite cycle over the
    persisted keys)."""
    home = _flat_write(tmp_path, {"wiki_in_rack": "true"})
    (home / "config.yaml").write_text(
        "plugins:\n  memrack:\n    enabled_backends: base-db\n",
        encoding="utf-8")
    mr = _spec_provider()
    prov1 = mr._MemrackBase()
    prov1.initialize(session_id="T037C1", hermes_home=str(home))
    stand1 = mr._norm_backends(prov1._config.get("enabled_backends"))
    flat_before = (home / "memrack" / "config.json").read_bytes()
    yaml_before = (home / "config.yaml").read_bytes()

    prov2 = mr._MemrackBase()
    prov2.initialize(session_id="T037C2", hermes_home=str(home))
    stand2 = mr._norm_backends(prov2._config.get("enabled_backends"))
    assert stand1 == stand2, (stand1, stand2)
    assert "wiki" in stand2
    assert (home / "memrack" / "config.json").read_bytes() == flat_before
    assert (home / "config.yaml").read_bytes() == yaml_before
    # the Basisfall guard: a home WITHOUT any toggle key folds nothing —
    # no fold materializes a defaulted stand into the live config
    plain_home = home / "plain-home"
    plain_home.mkdir(parents=True)
    plain = mr._MemrackBase()
    plain.initialize(session_id="T037B", hermes_home=str(plain_home))
    assert "enabled_backends" not in plain._config, plain._config
    assert not [k for k in plain._config if k.endswith("_in_rack")], plain._config


def test_gear_string_repr_folds_on_without_pane_field(tmp_path, monkeypatch):
    """The pane's membership select is gone from the DECLARED schema (the
    toggle family is its only membership UI) while the store repr keeps
    its shape: the toggle defaults still seed FROM the ``enabled_backends``
    string, and acting a pane toggle folds the ONE consistent comma string
    (dedupe, floor law) — a field missing from the pane changes nothing
    about the fold, and combined-stand strings can no longer contradict
    the toggles' individual state, because the pane no longer renders one."""
    # the pane: NO select field, defaults still derive from the string stand
    mod = _load_schema(tmp_path, monkeypatch, module_name="_t038_repr",
                       config_json={"enabled_backends":
                                    "base-db,state-episodic,wiki"})
    keys = {f.key for f in mod.CONFIG_SCHEMA.fields}
    assert "enabled_backends" not in keys, keys
    by = _fields_by_key(mod.CONFIG_SCHEMA)
    assert by["state-episodic_in_rack"].default == "true", by
    assert by["wiki_in_rack"].default == "true", by
    # the fold: the persisted raw toggle keeps maintaining the string repr
    # (a string in, the consistent string out — never a list, never a reset)
    home = _flat_write(tmp_path, {"enabled_backends":
                                  "base-db,state-episodic,wiki",
                                  "state-episodic_in_rack": "false"})
    mr = _spec_provider()
    prov = mr._MemrackBase()
    prov.initialize(session_id="T038S", hermes_home=str(home))
    assert prov._config.get("enabled_backends") == "base-db,wiki", (
        prov._config)
    assert isinstance(prov._config.get("enabled_backends"), str), (
        prov._config)
