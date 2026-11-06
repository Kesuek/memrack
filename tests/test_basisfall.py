"""T-013 VOLLSTAND (third-man S-1/B-3): the Basisfall is PROVEN, not claimed.

Stage ladder from SPEC §1.4 — each stage as its own test, each with a real provable
edge. A fresh HERMES_HOME with ONLY base-db must carry everything.
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


def _fresh(tmp):
    prov = mr._MemrackBase()
    prov.initialize(session_id="S1", hermes_home=str(tmp))
    return prov


def test_stage0_basisfall_solo(tmp_path):
    """Fresh home + ONLY base-db → write/search/status green (the core claim)."""
    prov = _fresh(tmp_path)
    # write
    out = json.loads(prov.handle_tool_call("memrack_write", {"content": "Relay laeuft auf LXC 903"}))
    assert out["status"] == "stored"
    # search (fan-out 'all' + targeted)
    solo = json.loads(prov.handle_tool_call("memrack_search", {"query": "Relay laeuft"}))
    assert solo["results"] and solo["results"][0]["target"]
    t = json.loads(prov.handle_tool_call("memrack_search", {"query": "Relay laeuft", "backend": "base-db", "mode": "fts"}))
    assert any(h["kind"] == "fact" for h in t["results"])
    # status: eine Card, geparktes wiki sichtbar, pending_docks leer
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "base-db" in st["cards"] and any(p["name"] == "wiki" for p in st["parked"])
    # B-7: prefetch lebt im Basisfall (kein Crash, kein Search-Noise-Spam)
    assert isinstance(prov.prefetch("anything"), str)
    block = prov.system_prompt_block()
    assert "memrack rack (v" in block and "- base-db:" in block
    assert "route_log" not in block


def test_stage1_docked_backend_broken_is_gap_not_death(tmp_path):
    """SPEC §1.4: ein kaputtes gedocktes Modul = gap-entry; DER REST läuft weiter."""
    (tmp_path / "plugins" / "memrack" / "backends.d").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugins" / "memrack" / "backends.d" / "broken.py").write_text(
        "raise RuntimeError('kaputt beim Import')\n", encoding="utf-8")
    prov = _fresh(tmp_path)
    assert prov._active.name == "base-db"
    assert any(g["backend"] == "broken" or "broken" in str(g) for g in prov._gaps) or prov._load_errors
    # der Rest des Racks (base-db) bleibt voll bedienbar
    out = json.loads(prov.handle_tool_call("memrack_write", {"content": "x"}))
    assert out["status"] == "stored"


def test_stage1b_init_failure_in_docked_backend(tmp_path):
    """docked Backend lädt AM ANFANG, aber initialize() explodiert → gap, Rest-Rack lebt."""
    d = tmp_path / "plugins" / "memrack" / "backends.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / "badmod.py").write_text(
        "class Bad:\n"
        "    name = 'badmod'\n"
        "    def initialize(self, session_id='', hermes_home=None, provider_config=None, **k):\n"
        "        raise RuntimeError('boom at init')\n"
        "    def on_turn(self, *a, **k): pass\n"
        "    def on_memory_write(self, *a, **k): pass\n"
        "    def on_session_end(self, *a, **k): pass\n"
        "    def prefetch(self, *a, **k): return ''\n"
        "    def system_prompt_block(self): return ''\n"
        "    def shutdown(self): pass\n"
        "    def export(self): return {}\n"
        "def export_backend():\n"
        "    return Bad()\n", encoding="utf-8")
    prov = mr._MemrackBase(config={"backend": "badmod"})   # FORCED target init-fail = fatal (Basisfall-Grenzfall)
    with pytest.raises(RuntimeError):
        prov.initialize(session_id="S1", hermes_home=str(tmp_path))
    # aber ohne forcing ist es nur ein Gap:
    prov2 = mr._MemrackBase()
    prov2.initialize(session_id="S1", hermes_home=str(tmp_path))
    assert prov2._active.name == "base-db"
    gap_names = {str(g.get("backend")) for g in prov2._gaps}
    assert "badmod" in gap_names


def test_stage2_vec_off_is_card_note_not_error(tmp_path):
    """SPEC §1.4+§3: vec ohne Engine → Card ohne 'vec', fts läuft, vec-Request = ehrlicher Error."""
    prov = mr._MemrackBase(config={"embedding_model": "model-gibt-es-nicht"})
    prov.initialize(session_id="S1", hermes_home=str(tmp_path))
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    card = st["cards"]["base-db"]
    assert "vec" not in card["modes"]                              # Card ohne vec (card_modes, ehrlich)
    assert prov._rack["base-db"].export()["vec"] is False          # Gap im Inventar sichtbar
    prov.handle_tool_call("memrack_write", {"content": "Fakt für fts"})
    solo = json.loads(prov.handle_tool_call("memrack_search", {"query": "Fakt für fts", "mode": "fts"}))
    assert solo["results"] != []
    ev = json.loads(prov.handle_tool_call("memrack_search", {"query": "x", "mode": "vec"}))
    assert "error" in ev   # specific-mode = ehrliche Fehler, kein stilles 0


def test_stage3_readonly_home_fails_closed(tmp_path):
    """SPEC §1.4 (third-man-fixiert): read-only plugin-data → Init scheitert ehrlich →
    frameworkseitiges Fallback auf builtin. Kein halbtoter Provider."""
    # Simulieren des nicht nutzbaren Pfads: OWNER-Verriegelung mit chmod rollt das engine-eigene
    # 0700 zurück — deshalb wir der Pfad als DATEI angelegt (mkdir/öffnen scheitert hart)
    base = tmp_path / "plugin-data" / "memrack"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("not a directory", encoding="utf-8")
    prov = mr._MemrackBase()
    with pytest.raises(RuntimeError):
        prov.initialize(session_id="S1", hermes_home=str(tmp_path))


def test_rrf_tiebreak_heat_then_freshness(tmp_path):
    """S-2: gleich-Score → heat DESC → ts DESC. Reihenfolge ist deterministisch und SPEC §3.3."""
    a = {"backend": "x", "kind": "fact", "target": "a", "content": "a", "rank": 1, "hits": 0, "ts": "2026-01-01"}
    b = {"backend": "x", "kind": "fact", "target": "b", "content": "b", "rank": 1, "hits": 3, "ts": "2026-12-01"}
    c = {"backend": "x", "kind": "fact", "target": "c", "content": "c", "rank": 1, "hits": 0, "ts": "2026-11-06"}
    merged = mr._MemrackBase._rrf_merge([[a], [b]], 8)
    assert [m["target"] for m in merged] == ["b", "a"]        # heat 3 > 0
    merged = mr._MemrackBase._rrf_merge([[a], [c]], 8)                     # same hits → ts desc: frischer zuerst
    assert [m["target"] for m in merged] == ["c", "a"]


def test_docstring_no_unproven_claim():
    src = pathlib.Path(_ROOT / "__init__.py").read_text(encoding="utf-8")
    assert "(T-013 proves it)" not in src   # die Zusage lebt als TEST, nicht als Claim
