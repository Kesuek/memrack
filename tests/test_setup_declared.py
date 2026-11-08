"""the DESKTOP config surface (finding: gear → Plugins → memrack showed
NO configuration). The desktop UI calls /config?surface=declared — it renders
ONLY from config_schema.py (CONFIG_SCHEMA, pure data) and writes storage=flat_json
(<hermes_home>/memrack/config.json). memrack so far reads ONLY plugins.memrack from
config.yaml — both stores must converge for RACKING.
"""
import importlib.util
import json
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr

# pure-data module of the core (dataclasses only) — needed to EXECUTE config_schema.py
_HERMES_TREE = pathlib.Path.home() / ".hermes" / "hermes-agent"
if str(_HERMES_TREE) not in sys.path:
    sys.path.insert(0, str(_HERMES_TREE))

# config_schema.py imports the runtime surface `plugins.memory.config_schema`
# (→ hermes_cli.config → hermes_yaml): without the Hermes tree those imports
# are unsatisfiable and the declared-schema tests cannot run. Skip at module
# top on fresh checkouts instead of failing — the bridge/wizard tests elsewhere
# (test_setup.py) carry no Hermes dependency and stay runnable everywhere.
pytest.importorskip(
    "plugins.memory.config_schema",
    reason="declared-surface tests need the Hermes runtime "
           "(plugins.memory.config_schema); fresh checkouts without the "
           "Hermes tree skip instead of failing")


def _load_declared():
    spec2 = importlib.util.spec_from_file_location(
        "_test_memrack_config_schema", _ROOT / "config_schema.py")
    mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(mod)
    return mod.CONFIG_SCHEMA


def _read_yaml(p):
    import yaml
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


# ── the declared surface itself ──────────────────────────────────────────────

def test_declared_schema_exists_and_is_sane():
    s = _load_declared()
    assert s.name == "memrack"
    assert s.storage == "flat_json"
    keys = {f.key for f in s.fields}
    assert {"backend", "auto_enable", "search_top_k",
            "embedding_model", "ollama_host"} <= keys
    assert "enabled_backends" not in keys   # the toggles are the pane's
    #                                        only membership UI; the string
    #                                        stays internal store repr


def test_declared_selects_are_closed_valid_sets():
    """allowed_values must CONTAIN the default (the desktop blanks unknown
    values). The pane's former closed membership select (`enabled_backends`)
    left with the toggle family — the remaining closed select is the
    force-backend override; the membership checks are bool toggles whose
    live contract the dynamic-surface laws pin."""
    s = _load_declared()
    by = {f.key: f for f in s.fields}
    be = by["backend"]
    assert be.kind == "select"
    assert {o.value for o in be.options} == {"", "base-db", "wiki"}
    assert be.default == ""


# ── the read bridge: profile_config must see BOTH stores ─────────────────────

def test_profile_config_flat_json_beats_yaml_per_key(tmp_path):
    import yaml
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  memrack:\n    enabled_backends: base-db,wiki\n    legacy_key: 1\n",
        encoding="utf-8")
    flat = tmp_path / "memrack" / "config.json"
    flat.parent.mkdir()
    flat.write_text(json.dumps({"enabled_backends": "base-db",
                                "search_top_k": 16}), encoding="utf-8")
    prov = mr._MemrackBase()
    merged = prov._profile_config(str(tmp_path))
    assert merged["enabled_backends"] == "base-db"   # desktop edit wins
    assert merged["search_top_k"] == 16              # flat-only key comes through
    assert merged["legacy_key"] == 1                 # yaml-only key stays


def test_profile_config_without_flat_json(tmp_path):
    import yaml
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  memrack:\n    enabled_backends: base-db,state-episodic\n",
        encoding="utf-8")
    prov = mr._MemrackBase()
    assert prov._profile_config(str(tmp_path)) == \
        {"enabled_backends": "base-db,state-episodic"}


def test_profile_config_yaml_broken_still_reads_flat_json(tmp_path):
    """A broken config.yaml must not kill the desktop config (silent failure)."""
    (tmp_path / "config.yaml").write_text("{ broken yaml!!", encoding="utf-8")
    (tmp_path / "memrack").mkdir()
    (tmp_path / "memrack" / "config.json").write_text(
        json.dumps({"backend": "base-db"}), encoding="utf-8")
    prov = mr._MemrackBase()
    assert prov._profile_config(str(tmp_path)) == {"backend": "base-db"}


# ── the wizard write mirrors into the desktop store (surfaces converge) ──

def test_save_config_writes_both_stores(tmp_path):
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db,wiki"}, str(tmp_path))
    block = _read_yaml(tmp_path / "config.yaml")["plugins"]["memrack"]
    assert block["enabled_backends"] == "base-db,wiki"
    flat = json.loads((tmp_path / "memrack" / "config.json").read_text(encoding="utf-8"))
    assert flat["enabled_backends"] == "base-db,wiki"
    mode = os.stat(tmp_path / "memrack" / "config.json").st_mode & 0o777
    assert mode == 0o600


def test_save_config_flat_json_merges_not_replaces(tmp_path):
    prov = mr._MemrackBase()
    flat = tmp_path / "memrack" / "config.json"
    flat.parent.mkdir()
    flat.write_text(json.dumps({"search_top_k": 16}), encoding="utf-8")
    prov.save_config({"enabled_backends": "base-db"}, str(tmp_path))
    merged = json.loads(flat.read_text(encoding="utf-8"))
    assert merged == {"search_top_k": 16, "enabled_backends": "base-db"}


# ── end to end: racking follows the DESKTOP store alone ──────────────────────

def test_initialize_racks_from_flat_json_alone(tmp_path):
    """A non-yaml store configures the rack — exactly the desktop path."""
    (tmp_path / "memrack").mkdir()
    (tmp_path / "memrack" / "config.json").write_text(
        json.dumps({"enabled_backends": "base-db,state-episodic"}),
        encoding="utf-8")
    prov = mr._MemrackBase()
    import sqlite3 as _sqlite3
    _c = _sqlite3.connect(str(tmp_path / "state.db"))
    _c.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp INTEGER, active INTEGER DEFAULT 1, content TEXT, tool_name TEXT, tool_args TEXT); CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, content='messages', content_rowid='id');")
    _c.commit(); _c.close()
    prov.initialize(session_id="S1", hermes_home=str(tmp_path))
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "state-episodic" in st["cards"]
