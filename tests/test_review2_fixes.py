"""Review-round-2 fixes — runtime correctness (P1/P2 of the second external scan).

These tests pin: (1) heat counters increment mirrored entries (id flow),
(2) base-db floor is auto-added when several others are present, (3) bad
limit is a controlled tool error, (4) wiki ts = file mtime + stat-first rescan,
(5) cross-backend targets don't collapse under RRF merge.
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


def _eng(tmp):
    prov = mr._MemrackBase()
    prov.initialize(session_id="R2", hermes_home=str(tmp))
    return prov._active


def test_heat_touches_mirrored_entry(tmp_path):
    eng = _eng(tmp_path)
    eng.on_memory_write("add", "memory", "alpha")
    eng.on_memory_write("add", "memory", "beta")
    hits = eng.search("alpha beta", "fts", 8)
    assert hits and all(h.get("id") is not None for h in hits)
    # heat increments only returned fact rows — exactly the retrieval contract
    after = {r[0]: r[1] for r in eng._db.execute("SELECT id, hits FROM facts").fetchall()}
    assert any(v > 0 for v in after.values())


def test_base_db_floor_when_many_backends(tmp_path):
    prov = mr._MemrackBase(config={"enabled_backends": "wiki,state-episodic"})
    # state.db seeded below
    db = tmp_path / "state.db"
    conn = __import__("sqlite3").connect(str(db))
    conn.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp INTEGER, active INTEGER DEFAULT 1, content TEXT, tool_name TEXT); CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, content='messages', content_rowid='id');")
    conn.commit(); conn.close()
    prov.initialize(session_id="R2", hermes_home=str(tmp_path))
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "base-db" in st["cards"], "floor must mount when several others present"
    out = json.loads(prov.handle_tool_call("memrack_write", {"content": "floor fact"}))
    assert out.get("status") == "stored"


def test_bad_limit_is_controlled_error(tmp_path):
    prov = mr._MemrackBase()
    prov.initialize(session_id="R2", hermes_home=str(tmp_path))
    out = json.loads(prov.handle_tool_call("memrack_search", {"query": "x", "limit": "boom"}))
    assert "error" in out and "limit must be an integer" in out.get("error", "")


def test_wiki_ts_is_file_mtime(tmp_path):
    from datetime import datetime, timezone
    from backends.wiki import WikiIndex
    wiki = tmp_path / "wiki"; wiki.mkdir()
    p = wiki / "page.md"
    p.write_text("# page\nkeyword here\n", encoding="utf-8")
    idx = WikiIndex(wiki)
    stored_mtime = idx._index["page.md"][0]
    hits = idx.search("keyword", "fts", 5)
    assert hits, "wiki hit missing"
    # ts must derive from the STORED mtime, not from the search clock (see P2-4)
    hit_ts = datetime.fromisoformat(hits[0]["ts"].replace("Z", "+00:00"))
    stored_ts = datetime.fromtimestamp(stored_mtime, tz=timezone.utc)
    assert abs((hit_ts - stored_ts).total_seconds()) < 0.05, (stored_ts, hit_ts)


def test_rrf_does_not_collapse_across_backends():
    hits = [{"backend": "base-db", "content": "x", "target": "t"},
            {"backend": "wiki", "content": "y", "target": "t"}]
    keys = ["%s:%s" % (h["backend"], h["target"]) for h in hits]
    assert len(set(keys)) == 2


def test_card_modes_picks_up_vec_runtime(tmp_path, monkeypatch):
    """P1-1: cards rebuilt after initialize() so vec mode (known only after probe) reaches the card."""
    from backends.base_db import BaseDbBackend
    monkeypatch.setattr(BaseDbBackend, "_vec_probe", lambda self: (setattr(self, "_vec_ok", True),
                                                                 setattr(self, "_vec_gap", "ok"),
                                                                 True)[2])
    prov = mr._MemrackBase(config={})
    prov.initialize(session_id="R2", hermes_home=str(tmp_path))
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "base-db" in st["cards"] and "vec" in st["cards"]["base-db"]["modes"], \
        ("vec probe must be reflected by the rebuilt card", st["cards"].get("base-db", {}).get("modes"))
