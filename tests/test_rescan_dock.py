"""T-029 bug 3 — a broken docked manifest must surface as a docking ERROR,
never as a pending 'registered (next session)' dock.

The rescan compared manifest FILE stems against backend NAMES while the load
errors key on their manifest `file` — so a broken manifest rode the pending
list forever (wrong promise: it will never register). Pinned: (a) broken
manifest => in docking_errors, not in pending_docks; (b) a loaded backend
whose manifest filename differs from the backend name is registered, not
pending; (c) after repairing the same manifest it registers cleanly.
"""
import importlib.util
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr

GOOD_MODULE = '''
class Svc:
    name = "myspecialbackend"

    def __init__(self):
        self.writes = []

    def initialize(self, session_id="", hermes_home=None, **kwargs):
        pass

    def on_turn(self, user, assistant, *, session_id=""):
        return None

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        self.writes.append((action, target, content))

    def on_session_end(self, messages, *, session_id=""):
        return None

    def prefetch(self, query, *, session_id=""):
        return ""

    def system_prompt_block(self):
        return ""

    def shutdown(self):
        pass

    def export(self):
        return {"name": self.name, "writes": len(self.writes)}


def export_backend():
    return Svc()
'''


def _dock(tmp_path):
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    dock.mkdir(parents=True, exist_ok=True)
    return dock


def _prov(tmp_path):
    prov = mr._MemrackBase(config={})
    prov.initialize(session_id="RESCAN", hermes_home=str(tmp_path))
    return prov


def _status(prov):
    return json.loads(prov.handle_tool_call("memrack_status", {}))


def test_broken_manifest_is_error_not_pending(tmp_path):
    dock = _dock(tmp_path)
    (dock / "broken.yaml").write_text(
        "name: brokensvc\nmodule: missing_mod.py\n", encoding="utf-8")
    status = _status(_prov(tmp_path))
    assert "brokensvc" not in status["backends"], "the broken dock must not register"
    assert any("broken.yaml" in str(e) for e in status["docking_errors"]), \
        status["docking_errors"]
    assert all(p["manifest"] != "broken.yaml" for p in status["pending_docks"]), \
        "a known-broken manifest must not pose as pending: %s" % status["pending_docks"]


def test_manifest_filename_differs_from_backend_name_still_registered(tmp_path):
    dock = _dock(tmp_path)
    (dock / "special_mod.py").write_text(GOOD_MODULE, encoding="utf-8")
    (dock / "special.yaml").write_text(
        "name: myspecialbackend\nmodule: special_mod.py\n", encoding="utf-8")
    status = _status(_prov(tmp_path))
    assert "myspecialbackend" in status["backends"], status["docking_errors"]
    assert all(p["manifest"] != "special.yaml" for p in status["pending_docks"]), \
        "a loaded manifest must not pose as pending: %s" % status["pending_docks"]


def test_repaired_manifest_registers_cleanly(tmp_path):
    dock = _dock(tmp_path)
    bad = dock / "broken.yaml"
    bad.write_text("name: brokensvc\nmodule: missing_mod.py\n", encoding="utf-8")
    status = _status(_prov(tmp_path))
    assert any("broken.yaml" in str(e) for e in status["docking_errors"])
    # repair: the SAME manifest now points at a real module in the dock dir
    (dock / "fixed_mod.py").write_text(
        GOOD_MODULE.replace("myspecialbackend", "brokensvc"), encoding="utf-8")
    bad.write_text("name: brokensvc\nmodule: fixed_mod.py\n", encoding="utf-8")
    status2 = _status(_prov(tmp_path))
    assert "brokensvc" in status2["backends"], status2["docking_errors"]
    assert all(p["manifest"] != "broken.yaml" for p in status2["pending_docks"])
    assert not any("broken.yaml" in str(e) for e in status2["docking_errors"]), \
        status2["docking_errors"]
