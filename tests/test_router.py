"""Router tests — pure logic, no Hermes imports needed."""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr


def test_route_table_known_types_target_wiki():
    assert mr.route(mr.EXPLICIT) == "wiki"
    assert mr.route(mr.EPISODIC) == "wiki"
    assert mr.route(mr.SEMANTIC) == "wiki"
    assert mr.route(mr.CURATION) == "wiki"


def test_route_unknown_type_falls_back_to_default():
    assert mr.route("nonsense") == mr.DEFAULT_BACKEND


def test_memory_write_actions_classify_as_explicit():
    table = dict(mr._MEMORY_WRITE_TYPES)
    assert table["add"] == mr.EXPLICIT
    assert table["patch"] == mr.EXPLICIT
    assert table["replace"] == mr.EXPLICIT
    assert table["remove"] == mr.EXPLICIT
    assert table.get("relevance_judgement") is None  # the Dreaming lesson


def test_pick_backend_matches_write_types():
    class Fts:
        name = "fts"
        write_types = ("explicit", "episodic")

    class Vec:
        name = "vec"
        write_types = ("semantic",)

    fts, vec = Fts(), Vec()
    assert mr.pick_backend([fts, vec], mr.SEMANTIC) is vec
    assert mr.pick_backend([fts, vec], mr.EXPLICIT) is fts
    assert mr.pick_backend([fts], mr.SEMANTIC) is None


def test_write_types_are_plain_strings():
    """Docked modules load via spec_from_file_location — value identity, not object identity."""
    for t in mr.ALL_TYPES:
        assert isinstance(t, str) and type(t) is str
