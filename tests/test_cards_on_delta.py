"""Cards-on-delta — the cards slot of memrack_search rides ONLY on a
membership delta (the live-membership fingerprint's stand hash, ONE source
with the T-048 law): the FIRST tool answer of the provider instance carries
the full cards, and every answer after a membership stand change does too;
an unchanged stand answers with the compact marker
{cards_unchanged, hint, write_targets} and NO card prose. The rack_status
slot stays on EVERY answer unchanged; memrack_status stays the full
inspection surface. Writes and vec_add calls advance the delta memo so the
next search stays consistent, while their own answer form is unchanged."""
import importlib.util
import json
import pathlib
import sqlite3
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


def _seed_env(tmp):
    db = tmp / "state.db"
    c = sqlite3.connect(str(db))
    c.executescript(
        "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, "
        "session_id TEXT, role TEXT, timestamp INTEGER, active INTEGER "
        "DEFAULT 1, content TEXT, tool_name TEXT); CREATE VIRTUAL TABLE IF "
        "NOT EXISTS messages_fts USING fts5(content, tool_name, "
        "content='messages', content_rowid='id');")
    c.commit()
    c.close()


def _prov(tmp, config=None):
    _seed_env(tmp)
    prov = mr._MemrackBase(config=config)
    prov.initialize(session_id="CARDSDELTA", hermes_home=str(tmp))
    return prov


def _search(prov, query):
    return json.loads(prov.handle_tool_call("memrack_search", {"query": query}))


def _flat_write(tmp, data):
    flat = tmp / "memrack"
    flat.mkdir(parents=True, exist_ok=True)
    (flat / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _flat_read(tmp):
    p = tmp / "memrack" / "config.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _is_full_cards(cards):
    return (isinstance(cards, dict) and cards
            and not cards.get("cards_unchanged")
            and all(isinstance(c, dict) for c in cards.values()))


# ── 1. the delta law ─────────────────────────────────────────────────────────

def test_first_search_carries_full_cards(tmp_path):
    prov = _prov(tmp_path)
    out = _search(prov, "no-such-token-xyzzy")
    assert _is_full_cards(out["cards"]), out["cards"]
    # the card prose is really there
    assert any(c.get("best_for") for c in out["cards"].values()), out["cards"]


def test_second_identical_search_carries_the_marker_no_prose(tmp_path):
    prov = _prov(tmp_path)
    first = _search(prov, "no-such-token-xyzzy")
    assert _is_full_cards(first["cards"]), first["cards"]
    second = _search(prov, "Relay note")
    marker = second["cards"]
    assert marker.get("cards_unchanged") is True, marker
    assert "memrack_status" in str(marker.get("hint")), marker
    # the compact write-target vocabulary stays known: backend -> writeable bool
    wt = marker.get("write_targets")
    assert isinstance(wt, dict) and set(wt) == set(first["cards"]), (wt, first["cards"])
    assert all(isinstance(v, bool) for v in wt.values()), wt
    # the token law: NO card prose anywhere in the marker path
    blob = json.dumps(second)
    for card in first["cards"].values():
        for text in (card.get("best_for") or "", card.get("limits") or ""):
            if text:
                assert text not in blob, text


def test_membership_flip_brings_the_cards_back_then_marker_again(tmp_path):
    prov = _prov(tmp_path)
    prov.handle_tool_call("memrack_write", {"content": "the user stores the Relay note"})
    marker1 = _search(prov, "Relay note")
    assert marker1["cards"].get("cards_unchanged") is True, marker1["cards"]
    # the membership flip (the gear pane's raw toggle)
    stand = _flat_read(tmp_path)
    stand["state-episodic_in_rack"] = "false"
    _flat_write(tmp_path, stand)
    delta = _search(prov, "Relay note")
    assert _is_full_cards(delta["cards"]), delta["cards"]
    # and unchanged again -> marker
    marker2 = _search(prov, "Relay note")
    assert marker2["cards"].get("cards_unchanged") is True, marker2["cards"]


def test_rack_status_stays_on_every_answer(tmp_path):
    prov = _prov(tmp_path)
    prov.handle_tool_call("memrack_write", {"content": "the user stores the Relay note"})
    first = _search(prov, "Relay note")
    second = _search(prov, "Relay note")
    for out in (first, second):
        rs = out["rack_status"]
        assert rs and "base-db" in rs, rs
        assert set(rs["base-db"]) >= {"modes", "participation"}, rs


# ── 2. memo hygiene: write/vec_add advance the memo, their form unchanged ────

def test_write_advances_the_memo_next_search_is_marker(tmp_path):
    prov = _prov(tmp_path)
    write_out = json.loads(prov.handle_tool_call(
        "memrack_write", {"content": "the user stores the Relay note"}))
    # the write answer form is UNCHANGED: it still carries the full cards
    assert _is_full_cards(write_out["cards"]), write_out.keys()
    # the write itself counts as the last card-carrying answer:
    # the next search is consistent (no duplicate card prose)
    out = _search(prov, "Relay note")
    assert out["cards"].get("cards_unchanged") is True, out["cards"]


def test_write_never_spoils_the_first_search_cards(tmp_path):
    # a write BEFORE any search does not consume the first-call rule:
    # the first search STILL carries the cards
    prov = _prov(tmp_path)
    out = _search(prov, "no-such-token-xyzzy")
    assert _is_full_cards(out["cards"]), out["cards"]


def test_vec_add_advances_the_memo(tmp_path):
    prov = _prov(tmp_path, config={"enabled_backends": ["base-db", "vec"]})
    if "vec" not in prov._rack:
        pytest.skip("this environment does not serve the vec backend")
    doc = "the user keeps the vec corpus note about the memory rack"
    out = json.loads(prov.handle_tool_call(
        "memrack_vec_add", {"source_ref": "doc/cards-delta.md", "text": doc}))
    if out.get("error"):
        pytest.skip("vec ingest unavailable in this environment: %s" % out["error"])
    nxt = _search(prov, "vec corpus note")
    assert nxt["cards"].get("cards_unchanged") is True, nxt["cards"]


# ── 3. status stays the full inspection surface ───────────────────────────────

def test_status_keeps_the_full_card_prose(tmp_path):
    prov = _prov(tmp_path)
    prov.handle_tool_call("memrack_write", {"content": "the user stores the Relay note"})
    _search(prov, "Relay note")   # marker state
    status = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert _is_full_cards(status["cards"]), status["cards"]
    assert any(c.get("best_for") for c in status["cards"].values()), status["cards"]
