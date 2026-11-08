"""Switch-consistency law for the uninstall grace — the store half.

Live defect pinned here (real house): a docked provider got uninstalled, the
grace law parked its card — but the flat json store kept claiming
``<name>_in_rack: true`` afterwards. The gear pane must never show "on" for
a provider the rack no longer hosts, and the toggles are the ONLY membership
keys in the stores (the membership dropdown left the pane): a stale on-key
against a parked card is a lie on every config surface, and worse — the
initialize fold reads the persisted keys and tried to UNPARK the parked card
on every session (the grace re-parked it afterwards: hidden rename churn).

Desired, in three rules:

1. Park -> switch off: when the grace law parks a docked card (at
   initialize() and on ``dock --refresh``), ``<name>_in_rack`` is written
   FALSE into BOTH config stores (`plugins.memrack` in config.yaml and the
   flat json ``<home>/<plugin>/config.json``) — merge per key, atomic 0600,
   the host config never clobbered (an unreadable yaml root is never
   "repaired"), churn-free when a store already sits at the target.
2. Re-install before the retention -> the card restores as recorded (the
   established presence law), but the switch STAYS OFF: the store keeps
   false, and the restore receipt carries the hint "flip the switch to
   reactivate" (only when the provider came back and the switch reads off
   after the write — a switch the user deliberately left ON keeps the plain
   presence restore, untouched).
3. A parked flagged card whose store switch still claims ON (pre-law
   stands) gets reconciled OFF — the store mirrors reality, the card file
   is never touched by the reconciliation.

The law never writes the ``enabled_backends`` string (internal store repr,
maintained by the initialize fold), never touches retention/flags/cleanup,
and stays INERT without the provider hub (it cannot tell an uninstall from
an unknown — the same fail-safe as the grace itself).

Ground rules (suite convention): no network, no host plugin registry, no
real catalog providers. Machine-level tests use the machine's OWN seams
(``loader``/``hub``) with in-process doubles; provider-level tests inject a
fake ``plugins.memory`` hub module (the machine's real import path —
monkeypatched per test) into scratch homes.
"""
import datetime as _dt
import importlib.util
import json
import pathlib
import sys
import types

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _spec_load(mod_name, relpath):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CS = _spec_load("memrack_config_surface", "memrack/config_surface.py")
DOCKING = _spec_load("memrack_docking_switch", "memrack/docking.py")
MR = _spec_load("memrack", "__init__.py")
sys.modules["memrack"] = MR
sys.modules["memrack_docking_core"] = DOCKING
sys.modules["memrack_config_surface"] = CS


class FakeProvider:
    """In-process duck double of a docked catalog provider (hermes-less)."""

    plugin_version = "9.9.9"

    def __init__(self, name="fakeprov"):
        self.name = name

    def is_available(self):
        return True

    def initialize(self, session_id="", **kwargs):
        pass

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


def _dock_home(tmp_path, provider_name):
    """Seed ONE reviewed dock card through the REAL machine flow (probe +
    chat-confirm review — no card invention here)."""
    fake = FakeProvider(provider_name)
    out = DOCKING.dock(provider_name, probe=True, loader=lambda: fake,
                       offers=[_offer(provider_name)],
                       hermes_home=str(tmp_path))
    assert out.get("status") == "draft-docked", out
    reviewed = CS.review_card(provider_name, "humans wrote this",
                              "reviewed limits", hermes_home=str(tmp_path))
    assert "error" not in reviewed, reviewed
    return fake


def _hub(dirs=(), entries=(), uninstalled=()):
    """A plugins-memory hub double (same shape the uninstall-grace tests
    build): names in ``dirs``/``entries`` count as installed, names in
    ``uninstalled`` load to None, everything else loads a fresh double."""
    dir_map = {n: pathlib.Path("/hub") / n for n in dirs}
    entry_map = {n: types.SimpleNamespace(name=n) for n in entries}

    def find_provider_dir(name):
        return dir_map.get(name)

    def find_provider_entry_point(name):
        return entry_map.get(name)

    def load_memory_provider(name, **kwargs):
        if name in dirs or name in entries or name not in uninstalled:
            return FakeProvider(name)
        return None

    hub = types.ModuleType("_fake_plugins_memory")
    hub.find_provider_dir = find_provider_dir
    hub.find_provider_entry_point = find_provider_entry_point
    hub.load_memory_provider = load_memory_provider
    hub.list_memory_provider_names = (
        lambda: sorted(set(dir_map) | set(entry_map) | set(uninstalled)))
    return hub


def _fake_hub_path(monkeypatch, hub):
    """Make the machine's REAL hub import (`from plugins import memory`)
    resolve to the double — the deployed wire, injected per test."""
    pkg = types.ModuleType("_fake_plugins_pkg")
    pkg.memory = hub
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "_fake_plugins_pkg.memory", hub)


def _flag_ts(days_ago, now=None):
    """The card's `uninstalled_at` shape, artificially aged."""
    base = (now or _dt.datetime.now(_dt.timezone.utc))
    ts = base - _dt.timedelta(days=days_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _init(tmp_path, config=None):
    prov = MR._MemrackBase(config=dict(config or {}))
    prov.initialize(session_id="SWITCH", hermes_home=str(tmp_path))
    return prov


def _status(prov):
    return json.loads(prov.handle_tool_call("memrack_status", {}))


def _flat(tmp_path):
    """The flat json store read back (the desktop's file — utf-8-sig)."""
    path = tmp_path / "memrack" / "config.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _yaml_block(tmp_path):
    """The yaml store's `plugins.<plugin>` block read back."""
    path = tmp_path / "config.yaml"
    if not path.is_file():
        return {}
    root = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(root, dict):
        return {}
    return dict((root.get("plugins") or {}).get("memrack") or {})


def _seed_switch_json(tmp_path, name, value):
    """Seed `<name>_in_rack` into the flat json the way the desktop's raw
    write lands (the pre-law live stand) — without the yaml store."""
    (tmp_path / "memrack").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "memrack" / "config.json"
    data = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    data["%s_in_rack" % name] = value
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _park_and_flag(tmp_path, name, *, age_days=0.0):
    """Simulate the grace's own park stand: card parked under the disabled-
    prefix, carrying `uninstalled_at` (no store switches seeded here)."""
    _dock_home(tmp_path, name)
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    (dock / ("%s.yaml" % name)).rename(dock / ("disabled-%s.yaml" % name))
    card_path = dock / ("disabled-%s.yaml" % name)
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    card["uninstalled_at"] = _flag_ts(age_days)
    card_path.write_text(
        yaml.dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return card_path


# ── rule 1: park flips the store switches off, in BOTH stores ───────────────

def test_init_park_flips_the_store_switches_off_in_both_stores(tmp_path,
                                                                monkeypatch):
    """The law's write half: a card parked by the grace at initialize()
    flips `<name>_in_rack` OFF in the flat json AND the yaml mirror —
    merge per key (unrelated keys and sibling plugin blocks survive), the
    card itself parked exactly as before (the established law), and the
    receipts name the switch work on the status surface."""
    _dock_home(tmp_path, "fakesvc")
    _seed_switch_json(tmp_path, "fakesvc", True)          # the live defect stand
    (tmp_path / "config.yaml").write_text(
        "plugins:\n"
        "  memrack:\n"
        "    search_top_k: 5\n"
        "  other-plugin:\n"
        "    keep: true\n",
        encoding="utf-8")

    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    prov = _init(tmp_path)

    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert not (dock / "fakesvc.yaml").exists(), "the card parks (unchanged law)"
    parked = yaml.safe_load((dock / "disabled-fakesvc.yaml").read_text(
        encoding="utf-8"))
    assert parked["uninstalled_at"], parked

    assert _flat(tmp_path).get("fakesvc_in_rack") is False, _flat(tmp_path)
    block = _yaml_block(tmp_path)
    assert block.get("fakesvc_in_rack") is False, block
    assert block.get("search_top_k") == 5, \
        "merge per key — the surviving store keys are never replaced: %s" % block
    root = yaml.safe_load((tmp_path / "config.yaml").read_text(
        encoding="utf-8-sig"))
    assert root["plugins"]["other-plugin"] == {"keep": True}, root

    entry = prov._uninstalled_report["parked"][0]
    assert entry["switch"]["member"] is False, entry
    switches = prov._uninstalled_report["switch_off"]
    assert [s["provider"] for s in switches] == ["fakesvc"], switches
    status = _status(prov)
    assert any(e["provider"] == "fakesvc"
               for e in status["switch_consistency"]), status


def test_refresh_park_flips_the_store_switches_off_too(tmp_path, monkeypatch):
    """`dock --refresh` parks through the same law: the store switches leave
    the rack together with the card, in both stores."""
    _dock_home(tmp_path, "limbic")
    _seed_switch_json(tmp_path, "limbic", True)
    _fake_hub_path(monkeypatch, _hub(uninstalled=["limbic"]))
    out = DOCKING.dock("limbic", refresh=True, hermes_home=str(tmp_path))
    assert out.get("status") == "uninstalled-parked", out
    assert out["switch"]["member"] is False, out
    assert _flat(tmp_path).get("limbic_in_rack") is False, _flat(tmp_path)
    assert _yaml_block(tmp_path).get("limbic_in_rack") is False, \
        _yaml_block(tmp_path)


# ── rule 2: re-install restores the card, the switch stays off ──────────────

def test_reinstall_keeps_the_switch_off_and_hints_the_flip(tmp_path,
                                                            monkeypatch):
    """The card comes back before the retention: restore without a re-probe
    (the established law), but the switch NEVER secretly turns on — both
    stores keep the off-key from the park, the restore receipt carries the
    'flip the switch to reactivate' note and the status surface serves it."""
    _dock_home(tmp_path, "fakesvc")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    first = _init(tmp_path)                                # parks + flips off
    assert _flat(first._hermes_home and tmp_path).get("fakesvc_in_rack") \
        is False, "the park wrote the off-stand"

    _fake_hub_path(monkeypatch, _hub(dirs=["fakesvc"]))    # installed again
    prov = _init(tmp_path)

    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert (dock / "fakesvc.yaml").exists(), "restored to the active name"
    card = yaml.safe_load((dock / "fakesvc.yaml").read_text(encoding="utf-8"))
    assert "uninstalled_at" not in card, card
    assert card["best_for"] == "humans wrote this", \
        "restore without re-probe — the recorded card text stays"
    assert _flat(tmp_path).get("fakesvc_in_rack") is False, _flat(tmp_path)
    assert _yaml_block(tmp_path).get("fakesvc_in_rack") is False, \
        _yaml_block(tmp_path)

    restored = prov._uninstalled_report["restored"][0]
    assert restored["provider"] == "fakesvc", restored
    assert "flip the switch to reactivate" in restored["note"], restored
    status = _status(prov)
    assert "fakesvc" in status["backends"], status["backends"]
    entry = next(e for e in status["restored_docks"]
                 if e["provider"] == "fakesvc")
    assert "flip the switch to reactivate" in entry["note"], entry


def test_reinstall_with_switch_on_is_the_plain_presence_restore(tmp_path,
                                                                monkeypatch):
    """The user deliberately kept the switch ON: the restore is the plain
    presence restore (unchanged law — the card racks in the SAME
    initialize), no switch write anywhere, no reactivate hint."""
    _park_and_flag(tmp_path, "fakesvc")
    _seed_switch_json(tmp_path, "fakesvc", True)
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  memrack:\n    fakesvc_in_rack: true\n", encoding="utf-8")
    _fake_hub_path(monkeypatch, _hub(dirs=["fakesvc"]))

    prov = _init(tmp_path)

    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert (dock / "fakesvc.yaml").exists(), "the card sits active again"
    status = _status(prov)
    assert "fakesvc" in status["backends"], status["backends"]
    assert _flat(tmp_path).get("fakesvc_in_rack") is True, \
        "the user's switch is NEVER written by the grace"
    assert _yaml_block(tmp_path).get("fakesvc_in_rack") is True
    assert status.get("restored_docks") is None, \
        "a switch-on restore is the plain presence path — no hint invented"
    flags = prov._uninstalled_report["kept"]
    assert any(e.get("flag_cleared") for e in flags), flags
    assert prov._uninstalled_report["switch_off"] == [], \
        "no switch-off write when the user's switch is on"


# ── rule 3: a parked card with a stale store switch reconciles off ──────────

def test_refresh_reconciles_a_parked_card_stale_switch(tmp_path, monkeypatch):
    """The pre-law live stand: parked + flagged card, store switch still ON.
    `dock --refresh` reads the lie and reconciles it — stores carry false
    afterwards, the card file is untouched (no card deletion, no re-flag)."""
    _park_and_flag(tmp_path, "fakesvc")
    _seed_switch_json(tmp_path, "fakesvc", True)
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  memrack:\n    fakesvc_in_rack: true\n", encoding="utf-8")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    card_before = (tmp_path / "plugins" / "memrack" / "backends.d" /
                   "disabled-fakesvc.yaml").read_bytes()

    out = DOCKING.dock("fakesvc", refresh=True, hermes_home=str(tmp_path))

    assert out.get("status") == "still-uninstalled", out
    assert out.get("switch_reconciled") is True, out
    assert out["switch"]["member"] is False, out
    assert _flat(tmp_path).get("fakesvc_in_rack") is False, _flat(tmp_path)
    assert _yaml_block(tmp_path).get("fakesvc_in_rack") is False
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert (dock / "disabled-fakesvc.yaml").exists(), "no card deletion"
    assert (dock / "disabled-fakesvc.yaml").read_bytes() == card_before, \
        "the reconciliation writes the store, never the card"


def test_init_heals_the_prewlaw_stand_without_touching_the_card(tmp_path,
                                                                 monkeypatch):
    """The live consolidation path: a parked flagged card from a pre-law
    session + the stale on-key in the flat json — one fresh initialize sets
    the store switch false in BOTH stores while the card keeps its parked
    file, its flag and its ORIGINAL clock (byte-identical card)."""
    _park_and_flag(tmp_path, "fakesvc")
    _seed_switch_json(tmp_path, "fakesvc", True)   # yaml store has NO key
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    card_before = (dock / "disabled-fakesvc.yaml").read_bytes()
    clock_before = yaml.safe_load(
        (dock / "disabled-fakesvc.yaml").read_text(encoding="utf-8"))[
        "uninstalled_at"]

    prov = _init(tmp_path)

    assert _flat(tmp_path).get("fakesvc_in_rack") is False, _flat(tmp_path)
    assert _yaml_block(tmp_path).get("fakesvc_in_rack") is False, \
        _yaml_block(tmp_path)
    assert (dock / "disabled-fakesvc.yaml").read_bytes() == card_before, \
        "the card file is byte-identical — only the stores were healed"
    parked = yaml.safe_load(
        (dock / "disabled-fakesvc.yaml").read_text(encoding="utf-8"))
    assert parked["uninstalled_at"] == clock_before, parked
    status = _status(prov)
    assert any(e["provider"] == "fakesvc"
               for e in status["switch_consistency"]), status
    visible = next(e for e in status["uninstalled_docks"]
                   if e["backend"] == "fakesvc")
    assert visible["uninstalled_at"] == clock_before, visible


def test_law_is_churnfree_on_a_healed_stand(tmp_path, monkeypatch):
    """Second initialize over the healed stand: no further switch writes
    (no mtime/byte noise in either store), no new entries invented."""
    _park_and_flag(tmp_path, "fakesvc")
    _seed_switch_json(tmp_path, "fakesvc", True)
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    _init(tmp_path)                                   # heals the stand
    flat = tmp_path / "memrack" / "config.json"
    json_before = flat.read_bytes()
    yaml_before = (tmp_path / "config.yaml")

    prov = _init(tmp_path)

    assert flat.read_bytes() == json_before, "the healed store is untouched"
    assert prov._uninstalled_report["switch_off"] == [], \
        "an idempotent re-init writes nothing: %s" % \
        prov._uninstalled_report["switch_off"]


# ── the boundaries ───────────────────────────────────────────────────────────

def test_no_hub_no_switch_writes(tmp_path, monkeypatch):
    """Fail-safe: hermes-less the law stays INERT — no park, no store write,
    the seeded stand stays exactly as it was."""
    monkeypatch.setattr(DOCKING, "_plugins_memory", lambda: None)
    _park_and_flag(tmp_path, "fakesvc")
    _seed_switch_json(tmp_path, "fakesvc", True)
    json_before = (tmp_path / "memrack" / "config.json").read_bytes()

    prov = _init(tmp_path)

    report = prov._uninstalled_report
    assert not (report["parked"] or report["switch_off"]), report
    assert (tmp_path / "memrack" / "config.json").read_bytes() == json_before, \
        "no hub = no store write"
    assert _flat(tmp_path).get("fakesvc_in_rack") is True, \
        "the seeded stand was never touched"


def test_yaml_store_failure_never_clobbers_the_host_config(tmp_path, monkeypatch,
                                                           caplog):
    """A config.yaml that parses to a non-mapping root is NEVER rewritten
    (the host config's business is not the grace's business) — the flat json
    still gets its key, the error is honest."""
    _dock_home(tmp_path, "fakesvc")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    (tmp_path / "config.yaml").write_text("- just\n- a list\n", encoding="utf-8")

    prov = _init(tmp_path)

    assert (tmp_path / "config.yaml").read_text(encoding="utf-8").strip() \
        == "- just\n- a list", "the host config was never clobbered"
    assert _flat(tmp_path).get("fakesvc_in_rack") is False, \
        "the flat store still got its switch"
    entry = prov._uninstalled_report["parked"][0]
    assert "error" in entry.get("switch_error", "") or \
        entry["switch"]["yaml"].get("error"), entry
    assert "fakesvc_in_rack" not in _yaml_block(tmp_path)


def test_store_switch_state_reads_json_over_yaml(tmp_path):
    """The reader mirrors `_profile_config` precedence: the flat json's keys
    WIN over the yaml block; absent keys return None (never a guess)."""
    (tmp_path / "memrack").mkdir(parents=True)
    (tmp_path / "memrack" / "config.json").write_text(
        '{"fakesvc_in_rack": true}', encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  memrack:\n    fakesvc_in_rack: false\n", encoding="utf-8")
    assert DOCKING.store_switch_state("fakesvc", hermes_home=str(tmp_path)) \
        is True, "json wins"

    (tmp_path / "memrack" / "config.json").unlink()
    assert DOCKING.store_switch_state("fakesvc", hermes_home=str(tmp_path)) \
        is False, "yaml-only stands read straight"

    (tmp_path / "config.yaml").write_text(
        "plugins:\n  other: {}\n", encoding="utf-8")
    assert DOCKING.store_switch_state("fakesvc", hermes_home=str(tmp_path)) \
        is None, "absent everywhere = None, never a default guess"
    assert DOCKING.store_switch_state("", hermes_home=str(tmp_path)) is None
