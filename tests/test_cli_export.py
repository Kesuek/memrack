"""T-029 bug 2 — `hermes memrack export` must not crash on the pointers schema.

The pointers table has NO `pointer` column (T-021 schema: subject, location,
kind, ts, checked) — the export SQL and its JSON key both said `pointer`.
Pinned: export against a seeded store exits 0, lists the seeded fact AND the
seeded pointer, and every pointer carries a `location` key.
"""
import argparse
import importlib.util
import json
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr

CLI = importlib.util.spec_from_file_location(
    "memrack_cli", pathlib.Path(_ROOT / "cli.py"))
cli = importlib.util.module_from_spec(CLI)
CLI.loader.exec_module(cli)


def test_export_no_crash_and_location_key(tmp_path, capsys, monkeypatch):
    fake_hc = types.ModuleType("hermes_constants")
    fake_hc.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hc)
    fake_pkg = types.ModuleType("plugins")
    fake_mem = types.ModuleType("plugins.memory")

    def fake_load(name, register_skills=True):
        return mr._MemrackBase(config={})   # the CLI initializes it itself
    fake_mem.load_memory_provider = fake_load
    fake_pkg.memory = fake_mem
    monkeypatch.setitem(sys.modules, "plugins", fake_pkg)
    monkeypatch.setitem(sys.modules, "plugins.memory", fake_mem)

    prov = mr._MemrackBase(config={})
    prov.initialize(session_id="CLI", hermes_home=str(tmp_path), platform="cli")
    prov.handle_tool_call("memrack_write", {"content": "Relay laeuft auf LXC 903"})
    prov._rack["base-db"].write_pointer("Relay", "~/wiki/main/entities/relay.md")
    prov.shutdown()

    rc = cli._run(argparse.Namespace(sub="export"))
    assert rc == 0, "export must not crash on the real pointers schema"
    out = json.loads(capsys.readouterr().out)
    assert out["facts"], "the exported facts must contain the seeded fact"
    assert out["pointers"], "the exported pointers must contain the seeded pointer"
    assert all("location" in p for p in out["pointers"]), out["pointers"]
    assert out["pointers"][0]["location"] == "~/wiki/main/entities/relay.md"
