"""the setup-consumer contract — get_config_schema wizard-ready + save_config
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
    import sqlite3 as _sqlite3
    _c = _sqlite3.connect(str(tmp_path / "state.db"))
    _c.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp INTEGER, active INTEGER DEFAULT 1, content TEXT, tool_name TEXT, tool_args TEXT); CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, content='messages', content_rowid='id');")
    _c.commit(); _c.close()
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
    assert data["plugins"]["other"] == {"x": 1}          # neighboring blocks remain
    assert data["memory"]["provider"] == "wiki-memory"   # activation remains


def test_save_config_merges_block_keeps_rest(tmp_path):
    """A wizard write MERGES the block: the sent key wins, prior keys survive.
    A replace would strand the desktop-only keys (search_top_k, ...) on every
    setup run."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("plugins:\n  memrack:\n    old_key: 1\n    enabled_backends: base-db\n",
                   encoding="utf-8")
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db,state-episodic"}, str(tmp_path))
    block = _read_yaml(cfg)["plugins"]["memrack"]
    assert block == {"old_key": 1,   # prior block keys survive
                     "enabled_backends": "base-db,state-episodic"}


def test_save_config_partial_wizard_write_keeps_search_top_k(tmp_path, monkeypatch):
    """A partial wizard send ({backend} only) must NOT drop a previously stored
    search_top_k — block merge, not replace. Uses the DEFAULT home resolution
    (monkeypatched HERMES_HOME) to exercise the exact path the wizard takes,
    and asserts the desktop mirror converges the same way."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db", "search_top_k": 16})
    prov.save_config({"backend": "base-db"})            # partial write: no top-k
    block = _read_yaml(tmp_path / "config.yaml")["plugins"]["memrack"]
    assert block["search_top_k"] == 16                  # survived the merge
    assert block["backend"] == "base-db"
    flat = json.loads(
        (tmp_path / "memrack" / "config.json").read_text(encoding="utf-8"))
    assert flat["search_top_k"] == 16                   # mirror merge agrees
    assert flat["backend"] == "base-db"


def test_save_config_host_yaml_atomic(tmp_path, monkeypatch):
    """The HOST config.yaml (the live Hermes config, not a plugin-owned file)
    is written atomically: tmp file in the same directory, then os.replace —
    never a direct write_text; no tmp is left behind and the rest of the
    config survives untouched."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  provider: wiki-memory\n", encoding="utf-8")
    import os
    calls = []
    real_replace = os.replace

    def _spy(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db"}, str(tmp_path))
    dst_names = sorted(pathlib.Path(d).name for _, d in calls)
    assert dst_names == ["config.json", "config.yaml"]   # both stores replaced
    for src, dst in calls:
        assert pathlib.Path(src).parent == pathlib.Path(dst).parent  # same dir
        assert pathlib.Path(src).name.endswith(".tmp")
    assert [p.name for p in tmp_path.iterdir()
            if p.name.endswith(".tmp")] == []            # no leftover tmp
    data = _read_yaml(cfg)
    assert data["memory"]["provider"] == "wiki-memory"   # host config intact
    assert data["plugins"]["memrack"] == {"enabled_backends": "base-db"}


def test_save_config_unwritable_home_no_crash(tmp_path, caplog):
    """Setup must never fail on persistence — failure = log, not exception."""
    block = tmp_path / "config.yaml"
    block.mkdir()                      # path is a DIR → writing fails
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db"}, str(tmp_path))   # no raise
