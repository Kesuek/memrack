"""In-flow backend cards — the three retrieval tools carry an additive
`cards` slot, rebuilt per call over the same card logic the init uses
(no stale init snapshot in tool answers). Pinned here: (a,b,c) the slot
lands on search/write/status answers, (d) parked backends never appear,
(e) an empty active rack omits the slot honestly, and a tampered init
cache can never leak into a tool answer.
"""
import importlib.util
import json
import pathlib
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


CARD_FIELDS = ("name", "best_for", "limits", "modes")


def _prov(tmp, config=None):
    prov = mr._MemrackBase(config=config)
    prov.initialize(session_id="INFLOW", hermes_home=str(tmp))
    return prov


def _card_ok(card):
    for field in CARD_FIELDS:
        assert field in card, "card misses %r: %s" % (field, card)
    assert card["name"], "card misses its name"


def test_search_answer_carries_cards(tmp_path):
    prov = _prov(tmp_path)
    prov.handle_tool_call("memrack_write", {"content": "Relay laeuft auf LXC 903"})
    out = json.loads(prov.handle_tool_call("memrack_search", {"query": "Relay laeuft"}))
    assert out.get("results"), "sanity: the seeded fact must be found"
    cards = out["cards"]
    assert "base-db" in cards, "the active floor backend must show its card"
    _card_ok(cards["base-db"])
    # additive: every pre-existing key of the tool answer is untouched
    assert set(out) >= {"query", "mode", "ms", "results", "engine_errors"}


def test_write_answer_carries_cards(tmp_path):
    prov = _prov(tmp_path)
    out = json.loads(prov.handle_tool_call(
        "memrack_write", {"content": "Fakten landen im Rack"}))
    assert out.get("status") == "stored", out
    cards = out["cards"]
    assert "base-db" in cards, "the write destination must show its card"
    _card_ok(cards["base-db"])
    assert out.get("status") == "stored"   # additive: write keys survive


def test_status_serves_rebuilt_cards(tmp_path):
    prov = _prov(tmp_path)
    prov._cards = {"ghost": {"name": "ghost"}}   # tamper the init-time snapshot
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "base-db" in st["cards"] and "ghost" not in st["cards"], \
        "status must serve the rebuilt rack cards, not the init cache"
    _card_ok(st["cards"]["base-db"])


def test_search_and_write_rebuild_not_cache(tmp_path):
    prov = _prov(tmp_path)
    prov._cards = {}   # tamper the init-time snapshot completely
    out = json.loads(prov.handle_tool_call("memrack_search", {"query": "Tamper Beweis"}))
    assert "base-db" in out.get("cards", {}), "search must rebuild cards on call"
    out = json.loads(prov.handle_tool_call("memrack_write", {"content": "Tamper Beweis"}))
    assert "base-db" in out.get("cards", {}), "write must rebuild cards on call"


def test_parked_backend_never_in_cards(tmp_path):
    prov = _prov(tmp_path, {"enabled_backends": "base-db"})   # wiki not enabled
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert any(p["name"] == "wiki" for p in st["parked"]), "sanity: wiki is parked here"
    for tool, args in (("memrack_status", {}),
                       ("memrack_search", {"query": "x"}),
                       ("memrack_write", {"content": "geparkt bleibt unsichtbar"})):
        cards = json.loads(prov.handle_tool_call(tool, args))["cards"]
        assert "wiki" not in cards, "parked backend leaked into the %s cards" % tool


def test_empty_rack_omits_cards_slot(tmp_path):
    prov = _prov(tmp_path)
    prov._rack, prov._cards = {}, {}   # degenerate rack: no active backends
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "cards" not in st, "no active backends -> no cards slot (honest)"
    out = json.loads(prov.handle_tool_call("memrack_search", {"query": "x"}))
    assert "cards" not in out, "empty rack answer must not carry cards"
    out = json.loads(prov.handle_tool_call("memrack_write", {"content": "x"}))
    assert "cards" not in out, "empty rack answer must not carry cards"
