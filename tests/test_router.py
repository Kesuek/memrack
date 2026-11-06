"""Router tests v0.3: the REAL router (_routed_for_kind) — the dead route()/ROUTE_TABLE
is removed (third-man A-1: the live router was untested, the tested one wrong)."""


import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


def _prov(rack, forced=None, forced_active=None):
    prov = mr._MemrackBase(config={"backend": forced} if forced else None)
    prov._rack = dict(rack)
    prov._force = bool(forced and forced in rack)
    forced_ok = bool(forced and forced in rack)
    prov._active = (forced_active or rack.get(forced)) if forced_ok else \
        (rack.get(forced) if forced else rack.get("base-db"))
    return prov


class B:  # stand-in backend: identity matters, not behavior
    def __init__(self, name):
        self.name = name


def test_tombstone_no_dead_router():
    """route()/ROUTE_TABLE entfernet — kein zweiter Router, kein Phantom-Endpoint."""
    assert not hasattr(mr, "route")
    assert not hasattr(mr, "ROUTE_TABLE")


def test_write_kinds_vocabulary_is_honest():
    """A-3: zwei Write-kinds; episodic ist routing-only; semantic ist eine SUCH-ART."""
    assert mr.WRITE_KINDS == ("explicit", "curated")
    assert not hasattr(mr, "SEMANTIC") and not hasattr(mr, "CURATION")
    assert not hasattr(mr, "ALL_TYPES")


def test_memory_write_actions_classify_as_explicit():
    table = dict(mr._MEMORY_WRITE_TYPES)
    assert table["add"] == mr.EXPLICIT
    assert table["patch"] == mr.EXPLICIT
    assert table["replace"] == mr.EXPLICIT
    assert table["remove"] == mr.EXPLICIT
    assert table.get("relevance_judgement") is None  # the Dreaming lesson


def test_forced_backend_beats_every_kind():
    rack = {"wiki": B("wiki"), "base-db": B("base-db")}
    prov = _prov(rack, forced="wiki")
    assert prov._routed_for_kind("explicit") is rack["wiki"]
    assert prov._routed_for_kind("episodic") is rack["wiki"]
    assert prov._routed_for_kind("curated") is rack["wiki"]


def test_episodic_routes_to_wiki_only_if_enabled():
    rack = {"base-db": B("base-db"), "wiki": B("wiki")}
    prov = _prov(rack)
    assert prov._routed_for_kind("episodic") is rack["wiki"]
    solo = _prov({"base-db": B("base-db")})   # wiki geparkt (default enabled)
    assert solo._routed_for_kind("episodic") is None   # unstored — state.db ist kanonisch


def test_explicit_and_curated_route_to_default_floor():
    rack = {"base-db": B("base-db"), "wiki": B("wiki")}
    prov = _prov(rack)
    assert prov._routed_for_kind("explicit") is prov._routed_for_kind("curated") is rack["base-db"]


def test_writes_go_to_exactly_one_backend():
    """Ledger-Gesetz: KEIN fan-out-write — jede Route-Antwort ist EIN Backend."""
    rack = {"base-db": B("base-db"), "wiki": B("wiki")}
    prov = _prov(rack)
    for kind in ("explicit", "curated"):
        r = prov._routed_for_kind(kind)
        assert r is not None and r is rack["base-db"]


def test_single_backend_rule_takes_the_lone_one():
    """Basisfall-Regel: rack len==1 → dorthin, egal wie es heist; falsch-forced fällt zurück."""
    prov = _prov({"solo": B("solo")})
    assert prov._pick_default_target() == "solo"
    prov2 = _prov({"base-db": B("base-db")}, forced="nosuch")
    assert prov2._pick_default_target() == "base-db"


def test_card_block_overflows_honestly():
    """A-2: kein stiller Cut — Overflow-Zeile nennt Zahl und memrack_status."""
    prov = mr._MemrackBase()
    prov._rack = {n: B(n) for n in [f"be{i:02d}" for i in range(10)]}
    prov._cards = {n: {"best_for": "x", "limits": "", "modes": ["fts"], "write": True,
                       "search": True, "built_in": False, "origin": "code"} for n in prov._rack}
    block = prov._card_block()
    assert "+2 weitere Backends — memrack_status" in block
    prov6 = mr._MemrackBase()
    prov6._rack = {n: B(n) for n in [f"b{i}" for i in range(6)]}
    prov6._cards = {n: {"best_for": "x", "limits": "", "modes": ["fts"], "write": True,
                        "search": True, "built_in": False, "origin": "code"} for n in prov6._rack}
    assert "weitere Backends" not in prov6._card_block()
