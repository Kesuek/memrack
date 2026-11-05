"""Registry + provider tests with a docked fake backend (no Hermes runtime)."""
import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr

FAKE_BACKEND = """
class Fake:
    name = "fake"
    write_types = ("explicit", "episodic", "semantic", "curation")

    def __init__(self):
        self.init_calls = []
        self.turns = None
        self.writes = []
        self.ended = False

    def initialize(self, session_id="", hermes_home=None, **kwargs):
        self.init_calls.append((session_id, hermes_home))

    def on_turn(self, user, assistant, *, session_id=""):
        self.turns = (user, assistant, session_id)

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        self.writes.append((action, target, content))

    def on_session_end(self, messages, *, session_id=""):
        self.ended = True

    def prefetch(self, query, *, session_id=""):
        return "FAKE-HIT: %s" % query

    def system_prompt_block(self):
        return "FAKE-SPB"

    def shutdown(self):
        pass

    def export(self):
        return {"name": self.name, "writes": len(self.writes)}


def export_backend():
    return Fake()
"""

INCOMPLETE_BACKEND = """
class Incomplete:
    name = "broken"

def export_backend():
    return Incomplete()
"""


def _dock(tmp_path, name, body):
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    dock.mkdir(parents=True, exist_ok=True)
    (dock / name).write_text(body, encoding="utf-8")


def test_dock_folder_convention_loads_valid_backend(tmp_path):
    _dock(tmp_path, "fake.py", FAKE_BACKEND)
    res = mr.discover_backends(tmp_path)
    # the built-in wiki module loads too (its hermes import happens lazily in initialize)
    assert "fake" in res["backends"]
    assert res["errors"] == []
    assert res["backends"]["fake"].name == "fake"


def test_dock_invalid_backend_is_skipped_not_crash(tmp_path):
    _dock(tmp_path, "broken.py", INCOMPLETE_BACKEND)
    res = mr.discover_backends(tmp_path)
    assert "broken" not in res["backends"]
    assert len(res["errors"]) == 1
    assert "misses" in res["errors"][0]["error"]


def test_dock_dir_missing_is_not_an_error(tmp_path):
    res = mr.discover_backends(tmp_path)
    import os
    errors = [e for e in res["errors"] if "wiki" not in e["file"]]
    assert errors == []


def test_provider_forced_backend_delegates_and_logs(tmp_path):
    _dock(tmp_path, "fake.py", FAKE_BACKEND)
    prov = mr._MemrackBase(config={"backend": "fake"})
    prov.initialize(session_id="S1", hermes_home=str(tmp_path))
    fake = prov._routed(mr.EXPLICIT)
    assert fake.name == "fake"
    assert fake.init_calls == [("S1", str(tmp_path))]

    prov.sync_turn("u", "a")
    prov.on_memory_write("add", "memory", "Ronny likes memrack")
    assert fake.turns == ("u", "a", "S1")
    assert fake.writes == [("add", "memory", "Ronny likes memrack")]
    assert prov.prefetch("wer") == "FAKE-HIT: wer"
    assert prov.system_prompt_block() == "FAKE-SPB"

    log = tmp_path / "plugin-data" / "memrack" / "route_log.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [r["kind"] for r in rows] == ["turn", "memory_write:add"]
    assert rows[0]["backend"] == "fake" and rows[0]["session_id"] == "S1"

    status = prov.status()
    assert status["active_backend"] == "fake" and status["forced_backend"] == "fake"
    assert "fake" in status["backends"]


def test_provider_default_route_all_targets_wiki_only_without_hermes(tmp_path):
    """No forced backend + no hermes runtime -> honest fail, never silent no-memory."""
    prov = mr._MemrackBase()
    with pytest.raises(RuntimeError):
        prov.initialize(session_id="S1", hermes_home=str(tmp_path))


def test_provider_tool_surface_is_additive(tmp_path):
    _dock(tmp_path, "fake.py", FAKE_BACKEND)
    prov = mr._MemrackBase(config={"backend": "fake"})
    prov.initialize(session_id="S1", hermes_home=str(tmp_path))
    names = [s["function"]["name"] for s in prov.get_tool_schemas()]
    assert "memrack_status" in names

    # status tool answers with real inventory
    assert prov.handle_tool_call("memrack_status", {}).startswith("{")

    # unknown tool -> explicit error, not a silent no-op
    import pytest as _p
    with _p.raises(KeyError):
        prov.handle_tool_call("nonexistent", {})


def test_backup_paths_declares_route_log(tmp_path):
    _dock(tmp_path, "fake.py", FAKE_BACKEND)
    prov = mr._MemrackBase(config={"backend": "fake"})
    prov.initialize(session_id="S1", hermes_home=str(tmp_path))
    paths = prov.backup_paths()
    assert len(paths) == 1 and paths[0].endswith("route_log.jsonl")
