"""T-007 wiki engine: in-memory page index — pure stdlib, no Hermes runtime needed."""
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

ENG = importlib.util.spec_from_file_location("memrack_wiki", pathlib.Path(_ROOT / "backends" / "wiki.py"))
wiki = importlib.util.module_from_spec(ENG)
ENG.loader.exec_module(wiki)


@pytest.fixture()
def idx(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "a.md").write_text("# Relay\n\nIOWAP-Cluster laeuft auf LXC 903.", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.md").write_text("# HA\n\nToken rotiert jaehrlich.\nRelay haengt dran.", encoding="utf-8")
    return wiki.WikiIndex(root, session_id="S1")


def test_fts_hits_page_with_target_and_snippet(idx):
    hits = idx.search("IOWAP LXC", "fts", 8)
    assert len(hits) == 1
    h = hits[0]
    assert h["kind"] == "page" and h["target"] == "wiki:a.md:L3"
    assert "IOWAP" in h["snippet"]


def test_and_semantics_excludes_pages_missing_a_token(idx):
    hits = idx.search("Token rotiert", "fts", 8)
    assert [h["target"] for h in hits] == ["wiki:sub/b.md:L3"]


def test_title_boost_orders_first(idx):
    hits = idx.search("Relay", "fts", 8)
    assert hits[0]["target"].startswith("wiki:a.md:")   # Title-Match schlaegt Body-only
    assert len(hits) == 2


def test_unknown_mode_raises(idx):
    with pytest.raises(ValueError):
        idx.search("x", "vec", 8)


def test_rescan_picks_up_new_page(idx):
    assert idx.pages == 2
    (idx.root / "c.md").write_text("# Neu\n\nFrischer Inhalt fuer den Pass.", encoding="utf-8")
    assert idx.rescan() == 1
    hits = idx.search("Frischer Inhalt", "fts", 8)
    assert hits and hits[0]["target"] == "wiki:c.md:L3"   # Erster Trefferzeilen-Hit = Body-Zeile


def test_deleted_page_leaves_index(idx):
    (idx.root / "a.md").unlink()
    idx.rescan()
    assert idx.pages == 1 and idx.search("IOWAP", "fts", 8) == []
