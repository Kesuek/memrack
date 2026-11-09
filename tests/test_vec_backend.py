"""The vec backend (split): live semantic recall over the ledger — OPTIONAL.

Skips entirely when the environment lacks sqlite-vec or nomic via ollama
(Basisfall must not depend on this). The floor (base-db) stays table-only.
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


def _pair(tmp_path):
    """Floor ledger (base-db writes) + the REAL vec backend (no patches)."""
    engspec = importlib.util.spec_from_file_location(
        "memrack_base_db", pathlib.Path(_ROOT / "backends" / "base_db.py"))
    base_db = importlib.util.module_from_spec(engspec)
    engspec.loader.exec_module(base_db)
    led = base_db.BaseDbBackend()
    led.initialize(session_id="S1", hermes_home=str(tmp_path))

    vecspec = importlib.util.spec_from_file_location(
        "memrack_backends_vec", pathlib.Path(_ROOT / "backends" / "vec.py"))
    VEC = importlib.util.module_from_spec(vecspec)
    vecspec.loader.exec_module(VEC)
    vb = VEC.VecBackend()
    vb.initialize(session_id="S1", hermes_home=str(tmp_path))
    return led, vb


def test_floor_card_excludes_vec(tmp_path):
    """The split law on the card: base-db = fts+pointers ONLY."""
    led, _ = _pair(tmp_path)
    assert "fts" in led.card_modes() and "pointers" in led.card_modes()
    assert "vec" not in led.card_modes()


def test_vec_card_reports_capability(tmp_path):
    _, vb = _pair(tmp_path)
    assert "vec" in vb.card_modes() if vb._vec_ok else "vec" not in vb.card_modes()


def test_vec_search_roundtrip(tmp_path):
    led, vb = _pair(tmp_path)
    led.write("explicit", "Der IOWAP Relay Cluster laeuft auf LXC 903 im Keller")
    led.write("explicit", "The user invests 25 Euro monthly in ETF plans")
    hits = vb.search("wo laeuft der AI Relay Cluster", "vec", 8)
    assert hits, "vec search must hit the paraphrase"
    assert "IOWAP Relay" in hits[0]["content"]


def test_vec_index_is_incremental(tmp_path):
    led, vb = _pair(tmp_path)
    led.write("explicit", "Erster Fakt fuer Embedding")
    vb.search("Erster Fakt", "vec", 8)                  # pass 1 embeds row 1
    led.write("explicit", "Zweiter Fakt spaeter dazu")
    hits = vb.search("Zweiter Fakt", "vec", 8)          # pass 2 embeds only the missing row
    assert hits and "Zweiter Fakt" in hits[0]["content"]
