"""legacy import job — builtin memory files (§-delimited) → fact rows, idempotent."""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr
import json


def _home_with_memories(tmp_path, memory_entries, user_entries=None):
    mem = tmp_path / "memories"
    mem.mkdir(parents=True, exist_ok=True)
    body = "\n§\n".join(memory_entries)
    (mem / "MEMORY.md").write_text(body, encoding="utf-8")
    if user_entries:
        (mem / "USER.md").write_text("\n§\n".join(user_entries), encoding="utf-8")
    return tmp_path


def _fresh_importer(home):
    prov = mr._MemrackBase()
    prov.initialize(session_id="IMP1", hermes_home=str(home))
    return prov


def test_import_both_files_stores_rows(tmp_path):
    home = _home_with_memories(tmp_path, [
        "Kommunikation: iMessage + Talk; Config nur per hermes config set",
        "Terminal cap ~420s foreground; lange Befehle background=true",
    ], user_entries=["The user works in Europe/Berlin; language German"])
    prov = _fresh_importer(home)
    out = json.loads(prov.handle_tool_call("memrack_import", {}))
    assert out["imported"] == 3 and out["duplicates"] == 0
    rows = json.loads(prov.handle_tool_call("memrack_search",
                                            {"query": "iMessage", "mode": "fts"}))["results"]
    assert rows and rows[0]["target"].startswith("builtin:MEMORY.md")


def test_import_is_idempotent(tmp_path):
    home = _home_with_memories(tmp_path, ["Ein stabiler Fakt ueber die Umgebung hier"])
    prov = _fresh_importer(home)
    first = json.loads(prov.handle_tool_call("memrack_import", {}))
    assert first["imported"] == 1
    second = json.loads(prov.handle_tool_call("memrack_import", {}))
    assert second["imported"] == 0 and second["duplicates"] == 1   # hash-dedupe, no duplicates
    facts = prov._rack["base-db"].export()
    assert facts["facts"] == 1


def test_import_filters_noise_and_missing_files(tmp_path):
    home = _home_with_memories(tmp_path, ["ok", "", "gueltiger eintrag lang genug"])
    prov = _fresh_importer(home)
    out = json.loads(prov.handle_tool_call("memrack_import", {"source": "memory"}))
    assert out["imported"] == 1            # noise (<10 chars / empty) filtered out
    out = json.loads(prov.handle_tool_call("memrack_import", {"source": "user"}))
    assert out["imported"] == 0 and any(f.get("note") == "not present" for f in out["files"])


def test_import_counts_against_the_floor_backend(tmp_path):
    home = _home_with_memories(tmp_path, ["Fakt A der importiert wird", "Fakt B ebenfalls"])
    prov = _fresh_importer(home)
    prov.handle_tool_call("memrack_import", {"source": "memory"})
    st = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert st["cards"]["base-db"]["write"] is True
