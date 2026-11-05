"""T-003: bridge-pattern manifest docking — 5 case classes + pending-dock rescan."""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


MODULE = (
    "class Docked:\n"
    "    name = 'db-copy'\n"
    "    best_for = 'code says X'\n"
    "    limits = 'code limit'\n"
    "    search_modes = ('fts',)\n"
    "    def search(self, query, mode, k, session_id=''): return []\n"
    "    def __init__(self): self.inited = False\n"
    "    def initialize(self, session_id='', hermes_home=None, provider_config=None, **k): self.inited = True\n"
    "    def on_turn(self, user, assistant, *, session_id=''): pass\n"
    "    def on_memory_write(self, action, target, content, metadata=None, *, session_id=''): pass\n"
    "    def on_session_end(self, messages, *, session_id=''): pass\n"
    "    def prefetch(self, query, *, session_id=''): return ''\n"
    "    def system_prompt_block(self): return ''\n"
    "    def shutdown(self): pass\n"
    "    def export(self): return {'name': self.name}\n"
    "def export_backend():\n"
    "    return Docked()\n"
)

STRUCT_LIE_MODULE = (
    "class Docked:\n"
    "    name = 'db-copy'\n"
    "    manifest = {}\n"
    "    search_modes = ('fts',)\n"
    "    def __init__(self): pass\n"
    "    def initialize(self, session_id='', hermes_home=None, provider_config=None, **k): pass\n"
    "    def on_turn(self, user, assistant, *, session_id=''): pass\n"
    "    def on_memory_write(self, action, target, content, metadata=None, *, session_id=''): pass\n"
    "    def on_session_end(self, messages, *, session_id=''): pass\n"
    "    def prefetch(self, query, *, session_id=''): return ''\n"
    "    def system_prompt_block(self): return ''\n"
    "    def shutdown(self): pass\n"
    "    def export(self): return {'name': self.name}\n"
    "def export_backend():\n"
    "    return Docked()\n"
)

def _dock(tmp_path, manifest_body, module_body=MODULE, module_name="docked_mod.py"):
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    dock.mkdir(parents=True, exist_ok=True)
    if module_body is not None:
        (dock / module_name).write_text(module_body, encoding="utf-8")
    (dock / "db-copy.yaml").write_text(manifest_body, encoding="utf-8")


def test_valid_manifest_docks_and_card_is_manifest_data(tmp_path):
    _dock(tmp_path, "name: db-copy\nmodule: docked_mod.py\nbest_for: manifest says Y\n"
                    "limits: manifest limit\nsearch: [fts]\n")
    res = mr.discover_backends(tmp_path, enabled_builtins=())
    assert list(res["backends"]) == ["db-copy"]
    card = mr._card(res["backends"]["db-copy"])
    # CARD IS DATA: the manifest text wins over the code attr (best_for='code says X')
    assert card["best_for"] == "manifest says Y"
    assert card["limits"] == "manifest limit"
    assert card["modes"] == ["fts"]


def test_disabled_prefix_is_parked_silently(tmp_path):
    _dock(tmp_path, "name: db-copy\nmodule: docked_mod.py\n")
    yaml_path = tmp_path / "plugins" / "memrack" / "backends.d" / "db-copy.yaml"
    disabled = yaml_path.parent / "disabled-db-copy.yaml"
    yaml_path.rename(disabled)   # bridge semantics: park without delete
    res = mr.discover_backends(tmp_path, enabled_builtins=())
    assert res["backends"] == {} and res["errors"] == []


def test_name_mismatch_is_a_registry_error(tmp_path):
    _dock(tmp_path, "name: other-name\nmodule: docked_mod.py\n")
    res = mr.discover_backends(tmp_path, enabled_builtins=())
    assert "db-copy" not in res["backends"]
    assert len(res["errors"]) == 1 and "other-name" in res["errors"][0]["error"]


def test_missing_module_is_a_registry_error(tmp_path):
    _dock(tmp_path, "name: db-copy\nmodule: does_not_exist.py\n", module_body=None)
    res = mr.discover_backends(tmp_path, enabled_builtins=())
    assert res["backends"] == {}
    assert len(res["errors"]) == 1 and "FileNotFoundError" in res["errors"][0]["error"]


def test_structural_lie_is_a_registry_error(tmp_path):
    # code claims search_modes but ships NO search callable → card-vs-code rejects
    _dock(tmp_path, "name: db-copy\nmodule: docked_mod.py\n", module_body=STRUCT_LIE_MODULE)
    res = mr.discover_backends(tmp_path, enabled_builtins=())
    assert res["backends"] == {}
    assert res["errors"] and "search" in res["errors"][0]["error"]


def test_status_shows_pending_dock_as_next_session(tmp_path):
    prov = mr._MemrackBase()
    prov.initialize(session_id="S1", hermes_home=str(tmp_path))   # nichts gedockt
    (tmp_path / "plugins" / "memrack" / "backends.d").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugins" / "memrack" / "backends.d" / "db-copy.yaml").write_text(
        "name: db-copy\nmodule: nope.py\n", encoding="utf-8")   # fs only, kein import
    pend = prov.status()["pending_docks"]
    assert len(pend) == 1
    assert pend[0]["note"] == "registered (next session)"
    assert pend[0]["manifest"] == "db-copy.yaml"
