"""T-014: the setup-consumer contract — get_config_schema wizard-ready + save_config
writes the SAME block _profile_config reads (plugins.memrack), str-convention like
wiki-memory (the wizard prompts TEXT; every list default/choice rides as a comma-string
and is normalized at read).
"""
import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


def _read_yaml(p: pathlib.Path):
    import yaml
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


# ── schema shapes ────────────────────────────────────────────────────────────

def test_schema_keys_present():
    keys = [f["key"] for f in mr._MemrackBase().get_config_schema()]
    for wanted in ("backend", "enabled_backends", "embedding_model",
                   "ollama_host", "search_top_k"):
        assert wanted in keys


def test_schema_is_wizard_shaped():
    """The wizard prompts text and writes strings; the web-UI kind-infers from the
    default type. Contract: defaults = str (list-keys) or int (counts → integer
    kind); NEVER a python list (repr garbage in selects), never bool-shaped int.
    The read-side _norm_backends repairs the type whatever the writer wrote."""
    for f in mr._MemrackBase().get_config_schema():
        assert f["key"], f
        d = f.get("default")
        assert d is None or (isinstance(d, (str, int)) and not isinstance(d, bool)), \
            (f["key"], d, type(d))
        for c in f.get("choices") or []:
            assert isinstance(c, str), (f["key"], c, type(c))
    topk = next(f for f in mr._MemrackBase().get_config_schema()
                if f["key"] == "search_top_k")
    assert isinstance(topk["default"], int)   # web-UI: kind=integer-Stepper


# ── read-side normalization (str from wizard, list from hand-edit — both work) ─

def test_norm_backends_string_comma_split():
    assert mr._norm_backends("base-db,wiki") == ["base-db", "wiki"]


def test_norm_backends_string_tolerates_spaces_and_case():
    assert mr._norm_backends(" base-db , wiki , state-episodic ") == \
        ["base-db", "wiki", "state-episodic"]


def test_norm_backends_list_passthrough_strs():
    assert mr._norm_backends(["base-db", "state-episodic"]) == ["base-db", "state-episodic"]


def test_norm_backends_empty_and_none_fall_to_default():
    assert mr._norm_backends("") == list(mr._DEFAULT_ENABLED)
    assert mr._norm_backends(None) == list(mr._DEFAULT_ENABLED)


def test_initialize_accepts_comma_string(tmp_path):
    """The wizard writes ONE string — initialize must rack base-db AND state-episodic."""
    prov = mr._MemrackBase(config={"enabled_backends": "base-db, state-episodic"})
    prov.initialize(session_id="S1", hermes_home=str(tmp_path))
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "state-episodic" in st["cards"]
    assert "base-db" in st["cards"]


# ── save_config: the wizard's write lands where _profile_config reads ────────

def test_save_config_writes_plugins_memrack_block(tmp_path):
    import yaml
    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  provider: wiki-memory\nplugins:\n  other:\n    x: 1\n",
                   encoding="utf-8")
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db,wiki"}, str(tmp_path))
    data = _read_yaml(cfg)
    assert data["plugins"]["memrack"] == {"enabled_backends": "base-db,wiki"}
    assert data["plugins"]["other"] == {"x": 1}          # Nachbar-Blocks bleiben
    assert data["memory"]["provider"] == "wiki-memory"   # Aktivierung bleibt


def test_save_config_replaces_block_keeps_rest(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("plugins:\n  memrack:\n    old_key: 1\n    enabled_backends: base-db\n",
                   encoding="utf-8")
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db,state-episodic"}, str(tmp_path))
    block = _read_yaml(cfg)["plugins"]["memrack"]
    assert block == {"enabled_backends": "base-db,state-episodic"}   # Block ersetzt, nicht append
    assert "old_key" not in block


def test_save_config_unwritable_home_no_crash(tmp_path, caplog):
    """Setup darf nie an der Persistenz scheitern — Fail = Log, nicht Exception."""
    block = tmp_path / "config.yaml"
    block.mkdir()                      # Pfad ist ein DIR → Schreiben scheitert
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db"}, str(tmp_path))   # no raise
