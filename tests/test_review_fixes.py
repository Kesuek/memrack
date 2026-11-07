"""Copilot-review round-1 fixes (memrack catalog PR #150373):

F1  the builtin-mirror used ONE shared target for every entry — a `remove`
    swept all siblings away (S-3 violation) and the RRF merge collapsed
    them. Now: per-entry targets (`builtin:<src>#<hash8>`), precise remove.
F2  every write ran a full FTS5 'rebuild' — the write path is incremental now
    (one new row indexed), the rebuild stays on cold paths (remove/pointer).
F3  single-column FTS joined tokens with implicit AND — long queries starved.
    Now: OR-join; bm25 rank absorbs the noise.
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


def _engine(tmp):
    prov = mr._MemrackBase()
    prov.initialize(session_id="S1", hermes_home=str(tmp))
    eng = prov._active
    assert eng.name == "base-db"
    return eng


# ── F1: mirror targets are per-entry, remove is precise ─────────────────────

def test_mirror_targets_per_entry(tmp_path):
    eng = _engine(tmp_path)
    eng.on_memory_write("add", "memory", "alpha entry")
    eng.on_memory_write("add", "memory", "beta entry")
    hits = eng.search("entry", "fts", 8)
    assert len(hits) >= 2                                   # 2 entries, NOT merged into one
    targets = set()
    for h in eng._db.execute("SELECT target FROM facts").fetchall():
        targets.add(h[0])
    assert len(targets) == 2                                # per-entry targets (hash8 suffixes)
    assert all(t.startswith("builtin:memory#") for t in targets)


def test_remove_spares_siblings(tmp_path):
    eng = _engine(tmp_path)
    eng.on_memory_write("add", "memory", "alpha entry")
    eng.on_memory_write("add", "memory", "beta entry")
    eng.on_memory_write("remove", "memory", "alpha entry")
    leftover = eng.search("entry", "fts", 8)
    assert len(leftover) == 1 and "beta" in leftover[0]["content"]
    assert eng._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


# ── F3: OR-recall on single-column fts ──────────────────────────────────────

def test_fts_or_keeps_recall_when_one_token_misses(tmp_path):
    eng = _engine(tmp_path)
    eng.write("explicit", "relay listens on port 8788")
    eng.write("explicit", "wiki curates the notepad daily")
    # one token miss must NOT starve the query (both facts are found via bm25 rank)
    hits = eng.search("relay gibtsnichttoken", "fts", 8)
    contents = [h["content"] for h in hits]
    assert any("relay" in c for c in contents)


def test_fts_or_rank_preferred_fact_first(tmp_path):
    """bm25 ranking does the precision work the old AND-gate lost."""
    eng = _engine(tmp_path)
    eng.write("explicit", "relay listens on the relay port 8788")
    eng.write("explicit", "the gateway restarts the tunnel")
    hits = eng.search("gateway relay tunnel", "fts", 8)
    contents = [h["content"] for h in hits]
    assert contents, "recall must not be empty"
    assert any("relay port" in c for c in contents[:2])      # the relay fact ranks up
