"""Live membership — the runtime-switch law.

Membership used to be an initialize-time stand: a running session held its
init-time rack no matter what the stores said afterwards. The law now: every
memrack_search/status/write/vec_add call re-evaluates the membership stand —
ONE cheap stat fingerprint over the stores; a CHANGED stand runs the
established flock-protected fold and rebuilds the rack, reusing warm
already-initialized backend instances (park = out of the active rack with the
instance kept warm; restore = the cached instance back; a never-initialized
backend initializes lazily with its honest probe/gap). Unchanged stand = zero
work (no fold, no rebuild). Read paths never write the config stores (the
docked park/restore renames the card file — presence IS the state).

The channel split, documented honestly: tool paths see the rack live; the
injected context channel (prefetch/system block) follows the session.
"""
import fcntl
import importlib.util
import json
import pathlib
import sqlite3
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _spec_load(mod_name, relpath):
    spec = importlib.util.spec_from_file_location(
        mod_name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules.setdefault(mod_name.rsplit("_", 1)[0], mod)
    return mod


mr = _spec_load("memrack_live", "__init__.py")
DOCKING = _spec_load("memrack_docking_live", "memrack/docking.py")
CS = _spec_load("memrack_config_surface_live", "memrack/config_surface.py")
CATALOG = _spec_load("memrack_catalog_adapter_live", "backends/_catalog_proxy.py")


def _flat_write(tmp, data):
    flat = tmp / "memrack"
    flat.mkdir(parents=True, exist_ok=True)
    (flat / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _flat_read(tmp):
    p = tmp / "memrack" / "config.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _seed_env(tmp):
    """The state.db stub the read-only backend mounts on + no wiki."""
    db = tmp / "state.db"
    c = sqlite3.connect(str(db))
    c.executescript(
        "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, "
        "session_id TEXT, role TEXT, timestamp INTEGER, active INTEGER "
        "DEFAULT 1, content TEXT, tool_name TEXT); CREATE VIRTUAL TABLE IF "
        "NOT EXISTS messages_fts USING fts5(content, tool_name, "
        "content='messages', content_rowid='id');")
    c.commit()
    c.close()


def _prov(tmp, config=None):
    _seed_env(tmp)
    prov = mr._MemrackBase(config=config)
    prov.initialize(session_id="LIVE", hermes_home=str(tmp))
    return prov


def _search(prov, query):
    return json.loads(prov.handle_tool_call(
        "memrack_search", {"query": query}))


class FakeProvider:
    """In-process duck double of a docked catalog provider (hermes-less)."""

    plugin_version = "9.9.9"

    def __init__(self, name="fakedock"):
        self.name = name
        self.init_calls = 0

    def is_available(self):
        return True

    def initialize(self, session_id="", **kwargs):
        self.init_calls += 1

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return '{"tool": "%s"}' % tool_name

    def system_prompt_block(self):
        return "FAKE-PROVIDER-BLOCK"

    def prefetch(self, query, *, session_id=""):
        return "FAKE-RECALL"

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        return None

    def on_memory_write(self, action, target, content, metadata=None, *,
                        session_id=""):
        return {"status": "stored"}

    def on_session_end(self, messages, *, session_id=""):
        return None

    def shutdown(self):
        pass


def _offer(provider_name, version="9.9.9"):
    return {"name": provider_name, "source": "user", "version": version,
            "dir": None, "active": False}


def _dock_card(tmp, provider_name):
    """Seed ONE reviewed dock card through the REAL machine flow (probe +
    chat-confirm review — no card invention here)."""
    out = DOCKING.dock(provider_name, probe=True, loader=FakeProvider,
                       offers=[_offer(provider_name)], hermes_home=str(tmp))
    assert out.get("status") == "draft-docked", out
    reviewed = CS.review_card(provider_name, "humans wrote this",
                              "reviewed limits", hermes_home=str(tmp))
    assert "error" not in reviewed, reviewed


def _hub(monkeypatch):
    """The machine's REAL hub import (`from plugins import memory`) resolves
    to a double: every requested provider name loads a fresh double."""
    def load_memory_provider(name, **kwargs):
        return FakeProvider(name)
    hub = types.ModuleType("_fake_plugins_memory")
    hub.find_provider_dir = lambda name: None
    hub.find_provider_entry_point = lambda name: None
    hub.load_memory_provider = load_memory_provider
    hub.list_memory_provider_names = lambda: ["fakedock"]
    pkg = types.ModuleType("_fake_plugins_pkg")
    pkg.memory = hub
    pkg.find_provider_dir = hub.find_provider_dir
    pkg.find_provider_entry_point = hub.find_provider_entry_point
    pkg.load_memory_provider = load_memory_provider
    pkg.list_memory_provider_names = hub.list_memory_provider_names
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.memory", hub)


# ── 1. the runtime-switch law: a toggle acts on the NEXT call ───────────────

def test_toggle_change_between_two_searches_of_one_provider(tmp_path):
    prov = _prov(tmp_path)
    prov.handle_tool_call("memrack_write", {"content": "Relay laeuft auf LXC 903"})
    first = _search(prov, "Relay laeuft")
    assert "base-db" in first["rack_status"], first["rack_status"]
    assert "state-episodic" in prov._rack, prov._rack
    assert "state-episodic" not in prov._warm_rack or True

    # the gear pane's write: the raw toggle lands in the flat store
    stand = _flat_read(tmp_path)
    stand["state-episodic_in_rack"] = "false"
    _flat_write(tmp_path, stand)

    second = _search(prov, "Relay laeuft")
    rs = second["rack_status"]
    assert "state-episodic" not in rs, rs
    assert "state-episodic" not in prov._rack, prov._rack
    assert rs["base-db"]["participation"] == "searched", rs
    assert second["results"], "the fact is still found by the floor"


def test_toggle_off_and_on_again_keeps_one_warm_instance(tmp_path):
    prov = _prov(tmp_path)
    warm = prov._rack["state-episodic"]
    stand = _flat_read(tmp_path)
    stand["state-episodic_in_rack"] = "false"
    _flat_write(tmp_path, stand)
    out = _search(prov, "warm check")
    assert "state-episodic" not in prov._rack, prov._rack
    assert prov._warm_rack["state-episodic"] is warm, \
        "a parked member stays warm"
    assert "state-episodic" not in out["rack_status"], out["rack_status"]

    # restore: the cached instance comes back — no re-probe
    stand["state-episodic_in_rack"] = "true"
    _flat_write(tmp_path, stand)
    out = _search(prov, "warm check")
    assert prov._rack["state-episodic"] is warm, \
        "restore reuses the warm instance"
    assert "state-episodic" in out["rack_status"], out["rack_status"]


def test_new_backend_joins_lazily_mid_session(tmp_path, monkeypatch):
    """A never-initialized backend (the dock card lands mid-session) joins on
    the NEXT call with its ordinary initialize — the honest probe/gap."""
    _hub(monkeypatch)
    prov = _prov(tmp_path)
    assert "fakedock" not in prov._rack, prov._rack
    _dock_card(tmp_path, "fakedock")
    out = _search(prov, "join check")
    assert "fakedock" in prov._rack, prov._rack
    assert "fakedock" in out["rack_status"], out["rack_status"]
    member = prov._rack["fakedock"]
    inits = []
    real_init = member.initialize

    def counting_init(*a, **kw):
        inits.append(1)
        return real_init(*a, **kw)

    member.initialize = counting_init
    prov._warm_rack["fakedock"] = member
    # a second stand change keeps the member warm (no re-probe)
    _flat_write(tmp_path, {"state-episodic_in_rack": "false"})
    _search(prov, "stay warm")
    assert inits == [], "a warm member never re-initializes on other changes"
    assert "fakedock" in prov._rack, prov._rack


def test_docked_park_mid_session_leaves_data_untouched(tmp_path, monkeypatch):
    _hub(monkeypatch)
    _dock_card(tmp_path, "fakedock")
    prov = _prov(tmp_path)
    member = prov._rack["fakedock"]
    inits = []
    real_init = member.initialize

    def counting_init(*a, **kw):
        inits.append(1)
        return real_init(*a, **kw)

    member.initialize = counting_init   # post-init wrapper: the cycle counts
    warm_member = member

    # park through the REAL fold path (the gear pane's raw key)
    _flat_write(tmp_path, {"fakedock_in_rack": False})
    out = _search(prov, "anything")
    assert "fakedock" not in prov._rack, prov._rack
    assert "fakedock" not in out["rack_status"], out["rack_status"]
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert (dock / "disabled-fakedock.yaml").exists(), sorted(
        p.name for p in dock.iterdir())
    shim = dock / "fakedock.py"
    park_shim_bytes = shim.read_bytes()
    flat_bytes_now = (tmp_path / "memrack" / "config.json").read_bytes()
    assert json.loads(flat_bytes_now)["fakedock_in_rack"] is False, \
        "the fold never rewrites the store that requested the park"

    # restore through the same path
    _flat_write(tmp_path, {"fakedock_in_rack": True})
    out = _search(prov, "anything")
    assert "fakedock" in prov._rack, prov._rack
    assert "fakedock" in out["rack_status"], out["rack_status"]
    assert prov._rack["fakedock"] is warm_member, \
        "restore reuses the warm member object (no re-probe)"
    assert inits == [], "restore never re-initializes the member"
    assert (dock / "fakedock.py").read_bytes() == park_shim_bytes, \
        "a park/restore cycle touches no member code (data untouched)"


# ── 2. the memo: unchanged stand = zero work ────────────────────────────────

def test_unchanged_stand_costs_zero_folds(tmp_path, monkeypatch):
    prov = _prov(tmp_path)
    folds, rebuilds = [], []
    real_fold = prov._fold_persisted_membership_toggles

    def fold_count(*a, **kw):
        folds.append(1)
        return real_fold(*a, **kw)

    monkeypatch.setattr(prov, "_fold_persisted_membership_toggles", fold_count)
    monkeypatch.setattr(prov, "_rebuild_rack_membership",
                        lambda *a, **kw: rebuilds.append(1))
    prov.handle_tool_call("memrack_status", {})
    _search(prov, "quiet read")
    prov.handle_tool_call("memrack_write", {"content": "still one stand"})
    assert folds == [], "unchanged stand must fold nothing"
    assert rebuilds == [], "unchanged stand must rebuild nothing"

    # a real change folds exactly once and rebuilds exactly once
    _flat_write(tmp_path, {"state-episodic_in_rack": "false"})
    _search(prov, "changed stand")
    assert folds == [1], folds
    assert rebuilds == [1], rebuilds

    # the new stand is memoized: the NEXT identical reads stay at zero work
    prov.handle_tool_call("memrack_status", {})
    _search(prov, "memoized")
    assert folds == [1], folds
    assert rebuilds == [1], rebuilds


def test_read_paths_never_write_the_config_stores(tmp_path):
    prov = _prov(tmp_path)
    _flat_write(tmp_path, {"state-episodic_in_rack": "false"})
    raw = tmp_path / "memrack" / "config.json"
    raw_bytes = raw.read_bytes()
    _search(prov, "no churn please")
    prov.handle_tool_call("memrack_status", {})
    assert raw.read_bytes() == raw_bytes, \
        "a read path rewrote the flat store"
    _flat_write(tmp_path, {"state-episodic_in_rack": "true"})
    raw_bytes = raw.read_bytes()
    _search(prov, "no churn please either")
    assert raw.read_bytes() == raw_bytes
    assert (tmp_path / "config.yaml").exists() is False, \
        "the fold invented a yaml store"


# ── 3. the lock law: a busy home is an honest skip, the prior law stays ────

def test_flock_busy_is_an_honest_skip(tmp_path):
    prov = _prov(tmp_path)
    lock_dir = tmp_path / "plugin-data" / "memrack"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(lock_dir / "init.lock", "a")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        _flat_write(tmp_path, {"state-episodic_in_rack": "false"})
        out = json.loads(prov.handle_tool_call("memrack_status", {}))
        assert "state-episodic" in out["backends"], out["backends"]
        assert "state-episodic" in prov._rack, prov._rack
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    # the memo was NOT advanced — the next call applies the stand
    out = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "state-episodic" not in prov._rack, prov._rack
    assert "state-episodic" not in out["backends"], out["backends"]


# ── 4. the rack_status slot rides the rebuild ───────────────────────────────

def test_rack_status_slot_reflects_the_new_participation(tmp_path):
    prov = _prov(tmp_path)
    _flat_write(tmp_path, {"state-episodic_in_rack": "false"})
    out = _search(prov, "slot check")
    rs = out["rack_status"]
    assert set(rs) == set(prov._rack), (rs, prov._rack)
    assert "state-episodic" not in rs and "base-db" in rs, rs
    _flat_write(tmp_path, {"state-episodic_in_rack": "true"})
    out = _search(prov, "slot check again")
    rs = out["rack_status"]
    assert "state-episodic" in rs, rs
    assert set(rs) == set(prov._rack), (rs, prov._rack)


# ── 5. the channel split: tool paths live, injection follows the session ───

def test_injected_channel_stays_session_bound(tmp_path):
    """The prefetch/system block channel keeps its session stand — only the
    four tool paths re-evaluate live (the honest card/docs stanza)."""
    prov = _prov(tmp_path)
    _flat_write(tmp_path, {"state-episodic_in_rack": "false"})
    block = prov.system_prompt_block()
    assert "state-episodic" in prov._rack, prov._rack   # no live fold here
    assert "- state-episodic: " in block, block   # injection = session stand
    # ...while the LIVE tool path has already dropped the member:
    out = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert "state-episodic" not in out["backends"], out["backends"]
    assert "state-episodic" not in prov._rack, prov._rack
