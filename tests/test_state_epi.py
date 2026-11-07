"""state-episodic backend: read-only engine over the session history (fake state.db).
The live state.db is NEVER touched by tests — the fixture imitates the schema
(messages + messages_fts external-content + epoch-float timestamps).
"""
import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr  # noqa: F811

ENG = importlib.util.spec_from_file_location("memrack_state_epi",
                                             pathlib.Path(_ROOT / "backends" / "state_epi.py"))
se = importlib.util.module_from_spec(ENG)
ENG.loader.exec_module(se)

FAKE_SCHEMA = """
CREATE TABLE messages(
  id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
  timestamp REAL, active INTEGER);
CREATE TABLE sessions(session_id TEXT, title TEXT);
CREATE VIRTUAL TABLE messages_fts USING fts5(
  content, content='messages', content_rowid='id');
"""

ROWS = [
    ("20261105_231913_9847ce", "user", "Wir deployen den IOWAP Relay Cluster auf LXC 903", 1790000000.123456, 1),
    ("20261105_231913_9847ce", "assistant", "Cluster laeuft — Relay auf 192.168.2.60 reachable", 1790000001.987654, 1),
    ("20261004_111132_d1876f", "user", "NovaForge Task an agent.ai geschickt", 1750000000.0, 1),
]


@pytest.fixture()
def eng(tmp_path):
    db = sqlite3.connect(tmp_path / "state.db")
    db.executescript(FAKE_SCHEMA)
    for sid, role, content, ts, act in ROWS:
        db.execute("INSERT INTO messages(session_id, role, content, timestamp, active)"
                   " VALUES(?,?,?,?,?)", (sid, role, content, ts, act))
    db.execute("INSERT INTO sessions(session_id, title) VALUES(?,?)",
               ("20261105_231913_9847ce", "IOWAP Relay Setup"))
    db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    db.commit(); db.close()
    e = se.StateEpisodicBackend()
    e.initialize(session_id="SE1", hermes_home=str(tmp_path))
    return e


def test_episodic_hit_references_turn_with_iso_ts(eng):
    hits = eng.search("IOWAP Relay Cluster", "episodic", 8)
    assert len(hits) == 2
    h = hits[0]
    assert h["kind"] == "episodic" and h["target"].startswith("session:20261105_231913_")
    assert h["target"].endswith("#1") or h["target"].endswith("#2")   # msg-id REFERENCE
    # A-5 law: NO raw epoch in ts — ISO UTC-Z microseconds
    import re as _re
    assert _re.fullmatch(r"2026-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", h["ts"]), h["ts"]


def test_read_only_connect_rejects_write(eng):
    # die ro-surface beweist sich: ein write-Versuch über die Verbindung scheitert
    with pytest.raises(sqlite3.OperationalError):
        eng._db.execute("INSERT INTO messages(session_id, role, content, timestamp, active)"
                        " VALUES('x','u','y',0,1)")


def test_unknown_mode_raises(eng):
    import pytest as _p
    with _p.raises(ValueError):
        eng.search("x", "vec", 8)


def test_hyphen_query_survives(eng):
    hits = eng.search("IOWAP-Relay", "episodic", 8)   # der hyphen-trap-Schutz
    assert len(hits) == 2


def test_no_state_db_is_honest_init_failure(tmp_path):
    e = se.StateEpisodicBackend()
    with pytest.raises(RuntimeError):
        e.initialize(session_id="X", hermes_home=str(tmp_path / "empty"))


def test_rack_includes_it_by_default(tmp_path):
    """T-023: die Session-Historie wird AUTOMATISCH mit durchsucht —
    default-enabled, kein extra-Config nötig."""
    assert "state-episodic" in mr._DEFAULT_ENABLED
    assert "episodic" in mr._SEARCH_MODES
