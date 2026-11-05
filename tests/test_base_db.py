"""Unit tests: built-in base-db engine (stdlib floor, SPEC §2/§1.4)."""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr

ENGINE = importlib.util.spec_from_file_location(
    "memrack_base_db", pathlib.Path(_ROOT / "backends" / "base_db.py"))
base_db = importlib.util.module_from_spec(ENGINE)
ENGINE.loader.exec_module(base_db)


@pytest.fixture()
def eng(tmp_path):
    e = base_db.BaseDbBackend()
    e.initialize(session_id="S1", hermes_home=str(tmp_path))
    return e


def test_write_and_dedupe(eng):
    first = eng.write("explicit", "HA Token rotiert 2026-11")
    assert first["status"] == "stored" and first["target"].startswith("base-db:")
    second = eng.write("explicit", "HA Token rotiert 2026-11")
    assert second["status"] == "duplicate" and second["id"] == first["id"]


def test_write_rejects_empty(eng):
    with pytest.raises(ValueError):
        eng.write("explicit", "   ")


def test_search_fts_hits_and_misses(eng):
    eng.write("explicit", "Relay LXC 903 auf 192.168.2.60")
    hits = eng.search("Relay LXC", "fts", 8)
    assert len(hits) == 1 and hits[0]["kind"] == "fact"
    assert hits[0]["content"].startswith("Relay LXC")
    assert eng.search("Mondlandung", "fts", 8) == []


def test_search_pointers_mode(eng):
    eng.write_pointer("Relay", "~/wiki/main/entities/iowap-relay.md", kind="wiki")
    hits = eng.search("Relay", "pointers", 8)
    assert len(hits) == 1 and hits[0]["kind"] == "pointer"
    assert "->" in hits[0]["content"]


def test_search_unknown_mode_raises(eng):
    with pytest.raises(ValueError):
        eng.search("x", "vec", 8)   # honest: this engine carries no vec


def test_touch_increments_hits(eng):
    r = eng.write("explicit", "Hit-me")
    eng.touch([r["id"], r["id"]])
    hits = eng.search("Hit-me", "fts", 8)
    assert hits  # row still there; heat counted twice without error


def test_on_memory_write_mirrors_builtin_target(eng):
    eng.on_memory_write("add", "memory", "Ronny mag sqlite", session_id="S1")
    hits = eng.search("Ronny mag sqlite", "fts", 8)
    assert hits and hits[0]["target"].startswith("builtin:")


def test_on_memory_write_remove(eng):
    eng.on_memory_write("add", "memory", "to be removed", session_id="S1")
    out = eng.on_memory_write("remove", "memory", "to be removed", session_id="S1")
    assert out["status"] == "removed"
    assert eng.search("to be removed", "fts", 8) == []


def test_card_fields_present():
    e = base_db.BaseDbBackend()
    assert e.name == "base-db"
    assert "fts" in e.search_modes and "pointers" in e.search_modes
    assert "vec" not in e.search_modes          # Basis-Dep: kein vec (SPEC §1.4)
    assert e.best_for and e.limits and e.writeable


def test_turns_are_never_copied(eng):
    eng.on_turn("user says hi", "assistant answers", session_id="S1")
    assert eng.search("user says hi", "fts", 8) == []   # episodic stays in state.db (SPEC §2)
