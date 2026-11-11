"""The rack-status slot — every memrack_search answer reports the rack LEAN:

per ACTIVE backend its live `modes` (same source as the cards — one copy,
no second truth) plus `participation` for THIS query: `searched` | `skipped`
(mode not carried) | `gap` (an engine_errors entry). Deterministic order,
NO card texts (the token law), and the slot rides every answer path,
including zero hits and honest-error searches. A companion contract pins
CLI-vs-init consistency: `memrack_status` (the CLI inventory pipeline) and
a fresh initialize deliver the SAME card set for the SAME home.
"""
import importlib.util
import json
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


def _prov(tmp, config=None):
    prov = mr._MemrackBase(config=config)
    prov.initialize(session_id="RACKSTATUS", hermes_home=str(tmp))
    return prov


def _slot_keys(out):
    rs = out["rack_status"]
    return [n for n in rs]


def test_search_answer_carries_rack_status(tmp_path):
    prov = _prov(tmp_path)
    prov.handle_tool_call("memrack_write", {"content": "Relay laeuft auf LXC 903"})
    out = json.loads(prov.handle_tool_call("memrack_search", {"query": "Relay laeuft"}))
    assert out.get("results"), "sanity: the seeded fact must be found"
    rs = out["rack_status"]
    assert "base-db" in rs, out
    st = rs["base-db"]
    assert st["modes"] == ["fts", "pointers"], st
    assert st["participation"] == "searched", st
    # lean: only the two lean keys, never a card text
    assert set(st) == {"modes", "participation"}, st


def test_slot_present_on_zero_hits_and_deterministic(tmp_path):
    prov = _prov(tmp_path)
    out = json.loads(prov.handle_tool_call(
        "memrack_search", {"query": "no-such-token-xyzzy"}))
    assert out.get("results") == []
    rs = out["rack_status"]
    assert list(rs) == sorted(list(rs)), "deterministic order (sorted names)"
    assert "base-db" in rs


def test_participation_skipped_when_mode_not_carried(tmp_path):
    # the vec backend is enabled but this env is not required to serve it:
    # targeting a mode vec does not carry must mark vec `skipped`, not `gap`
    prov = _prov(tmp_path, config={"enabled_backends": ["base-db", "vec"]})
    prov.handle_tool_call("memrack_write", {"content": "Nur ein Fakt fuer fts"})
    out = json.loads(prov.handle_tool_call(
        "memrack_search", {"query": "Fakt", "mode": "fts"}))
    rs = out["rack_status"]
    assert set(rs) == {"base-db", "vec"}, rs
    assert rs["vec"]["participation"] == "skipped", rs
    assert rs["base-db"]["participation"] == "searched", rs
    # the whole rack shows in the slot even on a targeted search
    assert set(rs) == {"base-db", "vec"}


def test_participation_gap_on_engine_error(tmp_path):
    prov = _prov(tmp_path)

    class Boom:
        def __init__(self, inner):
            self._inner = inner
            self.manifest = getattr(inner, "manifest", None)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def search(self, query, mode, k):
            raise RuntimeError("fanout exploded")

    wrapped = Boom(prov._rack["base-db"])
    prov._rack = {"base-db": wrapped}
    out = json.loads(prov.handle_tool_call(
        "memrack_search", {"query": "Relay"}))
    assert out["engine_errors"], out
    assert out["results"] == [], out
    rs = out["rack_status"]
    assert rs["base-db"]["participation"] == "gap", rs


def test_slot_leans_against_card_texts(tmp_path):
    prov = _prov(tmp_path)
    out = json.loads(prov.handle_tool_call("memrack_search",
                                           {"query": "Nichts gefunden hier"}))
    cards = out["cards"]
    blob = json.dumps(out["rack_status"])
    for bname, card in cards.items():
        for text in (card.get("best_for") or "", card.get("limits") or ""):
            if text:
                assert text not in blob, (bname, text)


def test_slot_on_honest_error_answers(tmp_path):
    prov = _prov(tmp_path)
    out = json.loads(prov.handle_tool_call(
        "memrack_search", {"query": "Relay", "mode": "no-such-mode"}))
    assert out.get("error"), out
    assert "rack_status" in out, out
    assert list(out["rack_status"]) == sorted(out["rack_status"]), out

    out2 = json.loads(prov.handle_tool_call(
        "memrack_search", {"query": "Relay", "backend": "unknown-be"}))
    assert out2.get("error"), out2
    assert "rack_status" in out2, out2

    out3 = json.loads(prov.handle_tool_call(
        "memrack_search", {"query": "Relay", "mode": "vec", "limit": 4}))
    assert out3.get("error"), out3   # the honest degradation law: no carrier
    assert "rack_status" in out3, out3


def test_cli_status_pipeline_matches_fresh_init_cards(tmp_path):
    """The CLI inventory pipeline (`memrack status` via handle_tool_call) and the
    cards of the SAME provider's just-initialized state must agree — one copy:
    the vec probe state is the same in both (deterministic env, no drift)."""
    prov = _prov(tmp_path, config={"enabled_backends": ["base-db", "vec"]})
    status = json.loads(prov.handle_tool_call("memrack_status", {}))
    fresh = prov._cards_now()
    assert set(status["cards"]) == set(fresh), (status["cards"], fresh)
    for name, card in fresh.items():
        served = status["cards"][name]
        assert served["modes"] == card["modes"], (name, served, card)
        assert served.get("declared") == card.get("declared"), (name, served, card)
    # and the search answer reports the slot from the SAME source
    out = json.loads(prov.handle_tool_call("memrack_search", {"query": "x-none-here"}))
    for name, card in fresh.items():
        assert out["rack_status"][name]["modes"] == card["modes"], (name, out["rack_status"])


def test_degraded_vec_card_carries_env_gap_in_both_pipelines(tmp_path):
    """A backend whose environment probe failed reports the reason on its card —
    in the fresh-init cards AND in the served status (the CLI pipeline): an
    empty `modes` list is never silent again (the card reports what IS)."""
    prov = _prov(tmp_path, config={"enabled_backends": ["base-db", "vec"]})
    vec = prov._rack["vec"]
    vec._vec_ok = False
    vec._vec_gap = "embedder unavailable in this test home"
    for backend in prov._rack.values():
        backend._vec_ok = getattr(backend, "_vec_ok", None)
    fresh = prov._cards_now()
    assert fresh["vec"]["modes"] == []
    assert fresh["vec"]["env_gap"] == "embedder unavailable in this test home", fresh["vec"]
    assert "env_gap" not in fresh["base-db"], fresh["base-db"]
    status = json.loads(prov.handle_tool_call("memrack_status", {}))
    served = status["cards"]["vec"]
    assert served["modes"] == [] and served.get("env_gap") == "embedder unavailable in this test home", served


def test_probe_failure_honest_degrade_in_both_pipelines(tmp_path, monkeypatch):
    """The env-dependency gap (a CLI pipeline whose interpreter lacks the optional
    package) must degrade the SAME way in both pipelines: fresh init and the
    status tool answer show the vec card without modes, with the honest reason —
    never a silent promise in one and silence in the other."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    import types

    def blocked_import(name, *a, **kw):
        if name == "sqlite_vec":
            raise ImportError("missing optional dep (simulated)")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", blocked_import)
    prov = mr._MemrackBase(config={"enabled_backends": ["base-db", "vec"]})
    prov.initialize(session_id="DEGRADE", hermes_home=str(tmp_path))
    fresh = prov._cards_now()
    if fresh["vec"]["modes"]:
        pytest.skip("this environment serves vec without the package — nothing to pin here")
    assert fresh["vec"]["env_gap"], fresh["vec"]
    status = json.loads(prov.handle_tool_call("memrack_status", {}))
    served = status["cards"]["vec"]
    assert served["modes"] == fresh["vec"]["modes"], (served, fresh["vec"])
    assert served.get("env_gap") == fresh["vec"].get("env_gap"), (served, fresh["vec"])
