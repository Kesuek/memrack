"""vec engine: OPTIONAL upgrade — sqlite-vec + nomic via ollama.
Skips entirely when the environment lacks them (Basisfall must not depend on this).
"""
import importlib.util
import json
import pathlib
import sys
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


try:
    import sqlite_vec          # noqa
    SQLITE_VEC = True
except ImportError:
    SQLITE_VEC = False


def _ollama_ok():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            tags = json.load(r)
        return "nomic-embed-text" in [str(m.get("name", "")).split(":")[0] for m in tags.get("models", [])]
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not SQLITE_VEC or not _ollama_ok(),
                                reason="env has no sqlite-vec or nomic — vec is OPTIONAL (the degradation law)")


def _engine(tmp_path):
    eng_spec = importlib.util.spec_from_file_location(
        "memrack_base_db", pathlib.Path(_ROOT / "backends" / "base_db.py"))
    base_db = importlib.util.module_from_spec(eng_spec)
    eng_spec.loader.exec_module(base_db)
    e = base_db.BaseDbBackend()
    e.initialize(session_id="S1", hermes_home=str(tmp_path))
    return e


def test_vec_card_reports_capability(tmp_path):
    e = _engine(tmp_path)
    assert "fts" in e.card_modes() and "pointers" in e.card_modes()
    assert "vec" in e.card_modes()


def test_vec_search_roundtrip(tmp_path):
    e = _engine(tmp_path)
    e.write("explicit", "Der IOWAP Relay Cluster laeuft auf LXC 903 im Keller")
    e.write("explicit", "The user invests 25 Euro monthly in ETF plans")
    hits = e.search("wo laeuft der AI Relay Cluster", "vec", 8)
    assert hits, "vec search must hit the paraphrase"
    assert "IOWAP Relay" in hits[0]["content"]


def test_vec_index_is_incremental(tmp_path):
    e = _engine(tmp_path)
    e.write("explicit", "Erster Fakt fuer Embedding")
    e.search("Erster Fakt", "vec", 8)                  # pass 1 embeds row 1
    e.write("explicit", "Zweiter Fakt spaeter dazu")
    hits = e.search("Zweiter Fakt", "vec", 8)          # pass 2 embeds only the missing row
    assert hits and "Zweiter Fakt" in hits[0]["content"]
