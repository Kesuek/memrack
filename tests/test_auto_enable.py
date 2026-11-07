"""auto-provision at activation — base-db + wiki can be auto-enabled when the
provider activates. Default OFF (nothing gets enabled without an explicit toggle).
The Basisfall stays intact: with the key absent/false the default rack is exactly
(base-db, state-episodic). state-episodic is untouched by the toggle (read-only,
default on).
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


def _fresh(tmp, config=None):
    prov = mr._MemrackBase(config=config or {})
    prov.initialize(session_id="S1", hermes_home=str(tmp))
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    return prov, set(st["cards"])


def test_default_off_rack_unchanged(tmp_path):
    """Absent key → EXACTLY the default rack (Basisfall law, untouched)."""
    _, cards = _fresh(tmp_path)
    assert cards == {"base-db", "state-episodic"}


def test_auto_enable_false_rack_unchanged(tmp_path):
    _, cards = _fresh(tmp_path, {"auto_enable": "false"})
    assert cards == {"base-db", "state-episodic"}


def test_auto_enable_true_adds_base_db_and_wiki(tmp_path):
    """The request: base-db + wiki get enabled at activation when toggled."""
    _, cards = _fresh(tmp_path, {"auto_enable": "true"})
    assert {"base-db", "wiki", "state-episodic"} <= cards


def test_truthy_variants_and_bool_native(tmp_path):
    for variant in (True, "true", "yes", "on"):
        _, cards = _fresh(tmp_path, {"auto_enable": variant})
        assert "wiki" in cards, variant
    for variant in (False, "false", "0", "", None):
        _, cards = _fresh(tmp_path, {"auto_enable": variant})
        assert "wiki" not in cards, variant


def test_auto_enable_never_disables(tmp_path):
    """A merge, not a replace: an explicit enabled list survives the toggle."""
    _, cards = _fresh(tmp_path, {"auto_enable": "true",
                                 "enabled_backends": "base-db"})
    assert {"base-db", "wiki"} <= cards


def test_desktop_roundtrip_flat_json(tmp_path):
    """The desktop writes the toggle into flat_json 0600 — the racking must follow."""
    (tmp_path / "memrack").mkdir()
    (tmp_path / "memrack" / "config.json").write_text(
        json.dumps({"auto_enable": True}), encoding="utf-8")
    _, cards = _fresh(tmp_path)
    assert {"base-db", "wiki"} <= cards


def test_schema_keys_carry_the_toggle():
    # wizard surface: str-convention with true/false choices, default off
    schema = {f["key"]: f for f in mr._MemrackBase().get_config_schema()}
    ae = schema.get("auto_enable")
    assert ae is not None
    assert ae["choices"] == ["true", "false"]
    assert ae["default"] == "false"
