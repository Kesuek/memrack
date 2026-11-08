"""P2 bundle — four small-behavior findings pinned BEFORE the fixes exist (RED):

1.  The persisted `dock_want` select is CONSUMED (one-shot receipt): after a
    success OR a controlled refusal the choice leaves BOTH config stores
    (dual-mirror, atomic, 0600) and the passed live config; the outcome stays
    visible as `dock_want_last` {provider, outcome, at} (additive on the
    status). A second initialize = no second probe (probe counter in the test).
2.  The wiki rescan is STAT-FIRST: a page whose mtime/size matches its index
    entry is never read; a deleted page leaves the index (tombstone, honestly
    counted); new/changed pages are read exactly once.
3.  The base-db remove's substring sweep becomes a CANDIDATE search: one
    match removes as before; MULTIPLE matches remove NOTHING and return
    `status: ambiguous` with the candidates (id/target/snippet).
4.  The RRF merge's targetless fallback key carries the backend identity
    (`backend:content`) — identical content out of different backends no
    longer collapses into one pool entry.
"""
import importlib.util
import json
import pathlib
import sys

import pytest  # noqa: E402
import yaml  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr  # noqa: E402


def _spec_load(mod_name, relpath):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CS = _spec_load("memrack_config_surface_p2", "memrack/config_surface.py")
BASE = _spec_load("memrack_base_db_p2", "backends/base_db.py")
WIKI = _spec_load("memrack_wiki_p2", "backends/wiki.py")


# ── shared doubles ───────────────────────────────────────────────────────────

class _ProbeSpyDocking:
    """Machine-module double that COUNTS the probe runs it receives."""

    def __init__(self, receipt=None):
        self.probes = []
        self._receipt = receipt or {"provider": "fakeprov",
                                    "status": "draft-docked", "spy": True}

    def dock(self, provider_name, **kwargs):
        self.probes.append((provider_name, dict(kwargs)))
        return dict(self._receipt)


def _seed_dock_want(tmp_path, choice="fakeprov"):
    """Seed the choice in BOTH stores (the desktop's flat json + the yaml
    block) — exactly the dual-mirror the provider reads on every initialize."""
    plugins_dir = tmp_path / "memrack"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    plugins_dir.chmod(0o700)
    (plugins_dir / "config.json").write_text(
        json.dumps({"dock_want": choice,
                    "search_top_k": 8}, indent=2), encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"plugins":
                   {"memrack": {"dock_want": choice,
                                "search_top_k": 8}}},
                  default_flow_style=False), encoding="utf-8")


def _read_stores(tmp_path):
    """The dual-mirror truth, read straight from the files."""
    out = {}
    flat = tmp_path / "memrack" / "config.json"
    if flat.exists():
        out["json"] = json.loads(flat.read_text(encoding="utf-8-sig"))
    conf = tmp_path / "config.yaml"
    if conf.exists():
        raw = yaml.safe_load(conf.read_text(encoding="utf-8-sig")) or {}
        out["yaml"] = (raw.get("plugins") or {}).get("memrack") or {}
    return out


# ── 1) dock_want consumed: success (probe counted) ──────────────────────────

def test_dock_want_consumed_once_after_success(tmp_path):
    """The persisted choice docks ONCE: the first consumption clears
    `dock_want` in BOTH stores (dual-mirror 0600, atomic) while keeping the
    receipt as `dock_want_last` {provider, outcome, at}; a second
    consumption over freshly-read store config runs NO second probe."""
    _seed_dock_want(tmp_path, "fakeprov")
    spy = _ProbeSpyDocking()
    live = {"dock_want": "fakeprov"}

    out = CS.dock_want_hook(live, hermes_home=str(tmp_path),
                            docking=spy)
    assert out.get("status") == "draft-docked", out
    assert len(spy.probes) == 1, spy.probes
    # the live config loses the choice and gains the one-shot receipt
    assert "dock_want" not in live, live
    last = live.get("dock_want_last") or {}
    assert last.get("provider") == "fakeprov", live
    assert last.get("outcome") == "draft-docked", live
    assert str(last.get("at") or "").endswith("Z"), live

    stores = _read_stores(tmp_path)
    for name in ("json", "yaml"):
        assert "dock_want" not in stores[name], (name, stores)
        got = stores[name].get("dock_want_last")
        assert got and got.get("provider") == "fakeprov", (name, stores)
        assert got.get("outcome") == "draft-docked", (name, stores)
        import os as _os
        for path in ((tmp_path / "config.yaml"),
                     (tmp_path / "memrack" / "config.json")):
            assert _os.stat(path).st_mode & 0o777 == 0o600, path

    # second consumption over freshly-read stores = honest no-op, no probe
    fresh = {"search_top_k": 8}   # what the stores hand back AFTER the clear
    fresh.update(stores.get("json") or {})
    fresh.pop("dock_want", None)
    out2 = CS.dock_want_hook(fresh, hermes_home=str(tmp_path), docking=spy)
    assert out2 is None, out2
    assert len(spy.probes) == 1, spy.probes


def _probe_count(tmp_path, spy):
    """One consumption pass of a REFUSED choice: spy never consulted, both
    stores cleared, the outcome recorded as refused."""
    (tmp_path / "plugins" / "memrack" / "backends.d").mkdir(
        parents=True, exist_ok=True)
    (tmp_path / "plugins" / "memrack" / "backends.d" /
     "disabled-fakesvc.yaml").write_text(
        "name: fakesvc\nmodule: fakesvc.py\n", encoding="utf-8")
    _seed_dock_want(tmp_path, "fakesvc")
    live = {"dock_want": "fakesvc"}
    out = CS.dock_want_hook(live, hermes_home=str(tmp_path), docking=spy)
    assert out.get("refused") is True, out
    assert spy.probes == [], spy.probes
    assert "dock_want" not in live, live
    assert live["dock_want_last"].get("outcome") == "refused", live
    return live


def test_dock_want_consumed_once_after_controlled_refusal(tmp_path):
    """A CONTROLLED refusal (parked card) is consumed too: the stale select
    clears in both stores with outcome 'refused' — it never re-asks."""
    spy = _ProbeSpyDocking()
    _probe_count(tmp_path, spy)
    stores = _read_stores(tmp_path)
    for name in ("json", "yaml"):
        assert "dock_want" not in stores[name], (name, stores)
        got = stores[name].get("dock_want_last")
        assert got and got.get("provider") == "fakesvc", (name, stores)
        assert got.get("outcome") == "refused", (name, stores)
    # a second pass is the plain no-op (no machine call, no store churn)
    out2 = CS.dock_want_hook({"dock_want_last": {"provider": "fakesvc"}},
                             hermes_home=str(tmp_path), docking=spy)
    assert out2 is None, out2
    assert spy.probes == [], spy.probes


def test_dock_want_empty_choice_never_touches_the_stores(tmp_path):
    """An absent or empty `dock_want` writes NOTHING (no config.yaml
    invention): the hook is the plain None no-op."""
    spy = _ProbeSpyDocking()
    assert CS.dock_want_hook({}, hermes_home=str(tmp_path),
                             docking=spy) is None
    assert CS.dock_want_hook({"dock_want": ""}, hermes_home=str(tmp_path),
                             docking=spy) is None
    assert spy.probes == [], spy.probes
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "memrack" / "config.json").exists()


def test_status_surfaces_dock_want_last_additive(tmp_path):
    """The one-shot receipt stays visible: the status carries
    `dock_want_last` exactly when the config holds it (additive slot)."""
    prov = mr._MemrackBase(config={
        "enabled_backends": "base-db",
        "dock_want_last": {"provider": "fakeprov",
                           "outcome": "draft-docked", "at": "X"}})
    prov.initialize(session_id="SB", hermes_home=str(tmp_path))
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert st["dock_want_last"] == {"provider": "fakeprov",
                                    "outcome": "draft-docked", "at": "X"}, st
    prov2 = mr._MemrackBase(config={"enabled_backends": "base-db"})
    prov2.initialize(session_id="SB2", hermes_home=str(tmp_path))
    st2 = json.loads(prov2.handle_tool_call("memrack_status", {}))
    assert "dock_want_last" not in st2, "absent receipt must stay absent"


def test_two_initializes_run_one_probe(tmp_path):
    """The provider-level proof: two consecutive initializes against the same
    home run the machine's probe EXACTLY once — the stores carry the
    choice's receipt, never the choice itself, after the first."""
    spy = _ProbeSpyDocking()
    _seed_dock_want(tmp_path, "fakeprov")
    prev = sys.modules.get("memrack_docking_core")
    sys.modules["memrack_docking_core"] = spy
    try:
        prov = mr._MemrackBase()
        prov.initialize(session_id="CI1", hermes_home=str(tmp_path))
        first = spy.probes
        prov.initialize(session_id="CI2", hermes_home=str(tmp_path))
        second = spy.probes
    finally:
        if prev is None:
            sys.modules.pop("memrack_docking_core", None)
        else:
            sys.modules["memrack_docking_core"] = prev
    assert first, "the first initialize runs the probe (the user's decision)"
    assert second == first, "a second initialize never re-probes (%s vs %s)" % (
        first, second)
    stores = _read_stores(tmp_path)
    for name in ("json", "yaml"):
        assert "dock_want" not in stores[name], (name, stores)
        assert stores[name].get("dock_want_last", {}).get("provider") == \
            "fakeprov", (name, stores)


# ── 2) wiki rescan: stat-first + tombstones ──────────────────────────────────

def _counting_read_text(monkeypatch):
    """Route pathlib.Path.read_text through a counting wrapper — the proof
    that rescan reads ONLY the pages it must."""
    real = pathlib.Path.read_text
    counter = {"reads": []}

    def wrapper(self, *args, **kwargs):
        counter["reads"].append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", wrapper)
    return counter


def _wiki(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "a.md").write_text("# Relay\n\nRelay facts live here.\n",
                               encoding="utf-8")
    (root / "b.md").write_text("# Token\n\nToken facts live here.\n",
                               encoding="utf-8")
    return WIKI.WikiIndex(root, session_id="P2")


def test_rescan_reads_only_new_or_changed_pages(tmp_path, monkeypatch):
    """Unchanged pages are NEVER read (stat-first): a rescan after touching
    exactly one page does one read, the next idle rescan does none."""
    idx = _wiki(tmp_path)
    counter = _counting_read_text(monkeypatch)

    (idx.root / "b.md").write_text("# Token\n\nToken facts REVISED here.\n",
                                   encoding="utf-8")
    before = len(counter["reads"])
    changed_count = idx.rescan()
    reads_one = counter["reads"].count(str(idx.root / "b.md"))
    reads_idle = counter["reads"].count(str(idx.root / "a.md"))
    assert reads_one == 1, (counter["reads"], reads_one)
    assert reads_idle == before and reads_idle == 0, (
        "the unchanged page must not be read", counter["reads"])
    assert changed_count == 1, changed_count
    report = idx.last_rescan
    assert report.get("changed") == ["b.md"], report
    assert report.get("reads") == 1, report

    before2 = len(counter["reads"])
    assert idx.rescan() == 0, "an idle rescan changes nothing"
    assert counter["reads"].count(str(idx.root / "b.md")) == 1, (
        "the idle pass must not re-read", counter["reads"][before2:])

    (idx.root / "c.md").write_text("# New\n\nFresh page arrives.\n",
                                   encoding="utf-8")
    assert idx.rescan() == 1
    assert counter["reads"].count(str(idx.root / "c.md")) == 1, counter["reads"]
    assert idx.last_rescan.get("new") == ["c.md"], idx.last_rescan


def test_rescan_tombstones_deleted_pages(tmp_path):
    """A deleted page leaves the index (tombstone) and is honestly counted —
    its hits are gone from the next search."""
    idx = _wiki(tmp_path)
    (idx.root / "a.md").unlink()
    out = idx.rescan()
    assert out == 1, out
    assert idx.pages == 1 and "a.md" not in idx._index, idx._index
    report = dict(idx.last_rescan)
    assert report.get("deleted") == ["a.md"], report
    hits = idx.search("Relay facts", "fts", 8)
    assert hits == [], hits


def test_rescan_counts_new_changed_deleted_together(tmp_path):
    """The pass count = new + changed + deleted (every index change honest);
    the breakdown rides along per pass."""
    idx = _wiki(tmp_path)
    (idx.root / "b.md").write_text("# Token\n\nToken facts REVISED here.\n",
                                   encoding="utf-8")
    (idx.root / "d.md").write_text("# Extra\n\nAnother fresh page.\n",
                                   encoding="utf-8")
    (idx.root / "a.md").unlink()
    assert idx.rescan() == 3, idx.last_rescan
    report = idx.last_rescan
    assert sorted(report["new"]) == ["d.md"], report
    assert sorted(report["changed"]) == ["b.md"], report
    assert sorted(report["deleted"]) == ["a.md"], report


# ── 3) base-db remove: substring containment ─────────────────────────────────

def _engine(tmp_path):
    eng = BASE.BaseDbBackend()
    eng.initialize(session_id="P2R", hermes_home=str(tmp_path),
                   provider_config={})
    return eng


def test_remove_substring_multi_match_removes_nothing(tmp_path):
    """A needle whose substring sweep finds MULTIPLE rows deletes NOTHING and
    returns status:ambiguous with the candidates for a targeted choice."""
    eng = _engine(tmp_path)
    r1 = eng.write("explicit", "the alpha one fact row")
    r2 = eng.write("explicit", "the alpha two fact row")
    eng.write("explicit", "an unrelated fact row")
    out = eng.on_memory_write("remove", "memory", "alpha")
    assert out["status"] == "ambiguous", out
    assert out["removed"] == 0 and out["rows"] == [], out
    candidates = out["candidates"] or []
    assert {c["id"] for c in candidates} == {r1["id"], r2["id"]}, candidates
    assert all("snippet" in c and "target" in c for c in candidates), candidates
    rows = eng._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    assert rows == 3, "an ambiguous needle must delete NOTHING"
    # the targeted exact removal still works afterwards
    out2 = eng.on_memory_write("remove", "memory", "the alpha two fact row")
    assert out2["status"] == "removed" and out2["removed"] == 1, out2


def test_remove_substring_single_match_still_removes(tmp_path):
    """ONE substring hit removes as before (honest match=substring report);
    the exact path is untouched."""
    eng = _engine(tmp_path)
    eng.write("explicit", "the lone beta fact row")
    eng.write("explicit", "an unrelated fact row")
    out = eng.on_memory_write("remove", "memory", "beta")
    assert out["status"] == "removed" and out["match"] == "substring", out
    assert out["removed"] == 1, out
    rows = eng._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    assert rows == 1, rows


# ── 4) RRF merge: the targetless fallback key carries the backend ────────────

def test_rrf_targetless_hits_across_backends_survive():
    """Identical content WITHOUT target out of DIFFERENT backends = two pool
    entries (the fallback key carries the backend identity); the same
    backend's own double-carry (two modes, one row) still collapses to one."""
    prov = mr._MemrackBase()

    def run(backend, content, mode="fts"):
        return [{"backend": backend, "kind": "fact", "rank": 1,
                 "id": 1, "content": content, "target": None,
                 "ts": "2026-11-08T00:00:00.000000Z", "hits": 0,
                 "mode": mode}]

    merged = prov._rrf_merge([run("base-db", "same content everywhere"),
                              run("wiki", "same content everywhere")], 8)
    assert len(merged) == 2, merged
    assert {e["backend"] for e in merged} == {"base-db", "wiki"}, merged

    one_backend = prov._rrf_merge([run("base-db", "one row many modes", "fts"),
                                   run("base-db", "one row many modes", "vec")], 8)
    assert len(one_backend) == 1, one_backend
    assert sorted(one_backend[0]["found_by"]) == ["fts", "vec"], one_backend
