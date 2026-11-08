"""Uninstall-grace law for docked providers — park first, remove after retention.

Live defect pinned here: a docked catalog provider whose install left the
house was silently dropped by the rack (no dock card action, no honest note,
an invisible missing member with an orphan card). Desired, in two stages:

1. At initialize() (and at `dock <provider> --refresh`): for EVERY docked
   card whose provider instance is no longer available (module gone /
   not loadable, WITH the install directory lost) AND that was a member
   before (machine dock evidence / review revisions on file):
   park immediately (the established `disabled-<name>.yaml` presence
   rename), set `uninstalled_at: <utc-now>` on the card and surface an
   honest note ("provider uninstalled — parked; card kept until ...").
   Never dock-error spam. A backend that is merely inactive (its install
   directory still exists — e.g. the plugins hub disabled flag) is NOT an
   uninstall: the existing park/gap laws keep applying, no flag.
2. Retention (`dock_retention_days`, default 14; 0 = immediate; -1 = never):
   at init, an `uninstalled_at` older than the retention removes the card +
   shim (backup per file under plugin-data/memrack/removed-cards/) with the
   note "removed card <name> after retention". A re-install before the
   retention lifts the park and drops the flag (restore without re-probe).
3. Status: docked-but-inactive (uninstalled-parked) cards surface in
   memrack_status (section `uninstalled_docks`, with the flag + days left),
   never as a silent absence.

Ground rules (suite convention): no network, no host plugin registry, no
real catalog providers. The machine-level tests use the machine's OWN
seams (`loader`, `hub`) with in-process doubles; the provider-level tests
inject a fake `plugins.memory` hub module (the machine's real import path —
monkeypatched per test) into scratch homes.
"""
import datetime as _dt
import importlib.util
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
DOCKING = _spec_load("memrack_docking_grace", "memrack/docking.py")
MR = _spec_load("memrack", "__init__.py")
sys.modules["memrack"] = MR


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
    chat-confirm review — no card invention here). Returns (home, fake)."""
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
    """A plugins-memory hub double: find_provider_dir / find_provider_entry_
    point / load_memory_provider — names in ``dirs``/``entries`` count as
    installed (dir/entry-point evidence), names in ``uninstalled`` load to
    None, everything else loads a fresh FakeProvider (the honest default:
    an installed provider loads)."""
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
    """The card's `uninstalled_at` shape, artificially aged (the retention
    sims write the flag with the module's OWN timestamp law)."""
    base = (now or _dt.datetime.now(_dt.timezone.utc))
    ts = base - _dt.timedelta(days=days_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _init(tmp_path, config=None):
    prov = MR._MemrackBase(config=dict(config or {}))
    prov.initialize(session_id="GRACE", hermes_home=str(tmp_path))
    return prov


def _status(prov):
    import json
    return json.loads(prov.handle_tool_call("memrack_status", {}))


# ── the install-loss probe (the law's gate) ─────────────────────────────────

def test_probe_detects_install_loss_but_not_a_disabled_install(monkeypatch):
    """The gate law: install loss = no dir, no pip entry point, no loadable
    instance. A merely INACTIVE install (directory present — the plugins hub
    disabled flag case — or merely a failing gate) is NOT an uninstall."""
    gone = DOCKING.uninstall_probe("limbic", hub=_hub(uninstalled=["limbic"]),
                                   loader=lambda: None)
    assert gone["installed"] is False, gone

    present = DOCKING.uninstall_probe("limbic", hub=_hub(dirs=["limbic"]),
                                      loader=lambda: None)
    assert present["installed"] is True, \
        "a present install dir is NOT an uninstall (existing laws): %s" % present

    entry = DOCKING.uninstall_probe("limbic", hub=_hub(entries=["limbic"]),
                                    loader=lambda: None)
    assert entry["installed"] is True, \
        "a pip entry-point install is NOT a dir-loss uninstall: %s" % entry

    live = DOCKING.uninstall_probe("limbic", hub=_hub(uninstalled=["limbic"]),
                                   loader=lambda: FakeProvider("limbic"))
    assert live["installed"] is True, live

    monkeypatch.setattr(DOCKING, "_plugins_memory", lambda: None)
    unknown = DOCKING.uninstall_probe("limbic", hub=None)
    assert unknown["installed"] is None, \
        "hub unavailable = cannot tell, the law must not act: %s" % unknown


def test_grace_gate_keys_on_machine_dock_cards_only():
    """The membership law ('... AND it was a member before'): the law acts on
    MACHINE-docked cards — probe evidence or review revisions on file. A hand
    bridge manifest (name/module only) is outside the law."""
    gateable_card = {"name": "fakesvc", "evidence": {"probed_at": "x"},
                     "revisions": []}
    bridge_card = {"name": "myspecialbackend", "module": "svc.py"}
    assert DOCKING._grace_gate(gateable_card) is True
    assert DOCKING._grace_gate(bridge_card) is False, \
        "a hand bridge manifest never earns the uninstall law"


# ── init detection: park + honest note, others intact ───────────────────────

def test_init_parks_uninstalled_member_with_flag_and_note(tmp_path, monkeypatch):
    """The live-defect law: card + shim on file, install left the house →
    at initialize() the card parks under the disabled- prefix (shim kept,
    untouched bytes), carries `uninstalled_at` + the honest kept-until note,
    the rack drops the member WITHOUT a dock error, and the honest status
    section names the parked card. Other, still-installed docks stay intact."""
    _dock_home(tmp_path, "fakesvc")
    # a second, still-installed dock card for the intact law
    _dock_home(tmp_path, "staysvc")

    hub = _hub(dirs=["staysvc"], uninstalled=["fakesvc"])
    _fake_hub_path(monkeypatch, hub)

    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    shim_before = (dock / "fakesvc.py").read_text(encoding="utf-8")

    prov = _init(tmp_path)
    assert (dock / "fakesvc.yaml").exists() is False, \
        "the uninstalled member's card must park"
    parked = yaml.safe_load((dock / "disabled-fakesvc.yaml").read_text(
        encoding="utf-8"))
    assert parked["uninstalled_at"], parked          # the flag
    assert "uninstalled — parked" in parked["uninstall_note"], parked
    assert "until" in parked["uninstall_note"], parked   # kept-until date
    assert (dock / "fakesvc.py").read_text(encoding="utf-8") == shim_before, \
        "the shim stays untouched (card edits never need a shim rewrite)"
    # no dock-error spam: the parked manifest never became an engine error
    status = _status(prov)
    assert status["docking_errors"] == [], status["docking_errors"]
    assert "fakesvc" not in status["backends"], status["backends"]
    assert "staysvc" in status["backends"], \
        "an installed sibling dock stays racked (intact law): %s" % status
    visible = status["uninstalled_docks"]
    entry = next(e for e in visible if e["backend"] == "fakesvc")
    assert entry["state"] == "parked", entry
    assert entry["days_left"] is not None and entry["days_left"] > 0, entry
    assert entry["uninstalled_at"] == parked["uninstalled_at"], entry


def test_init_leaves_a_disabled_install_and_bridge_docks_alone(tmp_path,
                                                               monkeypatch):
    """Two non-uninstalls in one home: (a) an install dir that still exists
    but fails to load (disabled flag case) — the card stays ACTIVE with no
    flag (the existing gap/degrade laws keep applying); (b) a hand-written
    bridge manifest — outside the law even when its name looks uninstalled."""
    _dock_home(tmp_path, "fakesvc")
    _dock_home(tmp_path, "disabledsvc")
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    (dock / "bridge.yaml").write_text(
        "name: bridgebackend\nmodule: bridge_mod.py\n", encoding="utf-8")
    (dock / "bridge_mod.py").write_text(
        "class Svc:\n"
        "    name = 'bridgebackend'\n"
        "    def initialize(self, session_id='', hermes_home=None, **kwargs):\n"
        "        pass\n"
        "    def on_turn(self, user, assistant, *, session_id=''):\n"
        "        return None\n"
        "    def system_prompt_block(self):\n"
        "        return ''\n"
        "    def shutdown(self):\n"
        "        pass\n"
        "    def export(self):\n"
        "        return {'name': self.name}\n"
        "    def prefetch(self, query, *, session_id=''):\n"
        "        return ''\n"
        "    def on_memory_write(self, action, target, content, metadata=None, *, session_id=''):\n"
        "        return None\n"
        "    def on_session_end(self, messages, *, session_id=''):\n"
        "        return None\n"
        "def export_backend():\n"
        "    return Svc()\n",
        encoding="utf-8")

    hub = _hub(dirs=["disabledsvc", "fakesvc"])   # both installs present on disk
    _fake_hub_path(monkeypatch, hub)

    prov = _init(tmp_path)
    status = _status(prov)
    assert not (dock / "disabled-fakesvc.yaml").exists(), \
        "a present install dir is not an uninstall — no park"
    card = yaml.safe_load((dock / "disabledsvc.yaml").read_text(
        encoding="utf-8"))
    assert "uninstalled_at" not in card, card
    assert "disabledsvc" in status["backends"], status
    assert "fakesvc" in status["backends"], \
        "the present install dir keeps its provider rackable: %s" % status
    assert "bridgebackend" in status["backends"], \
        "a hand bridge manifest is outside the law (presence is its state)"
    assert status["uninstalled_docks"] == [], status["uninstalled_docks"]
    assert not prov._uninstalled_report["parked"], prov._uninstalled_report


def test_init_hermes_less_never_acts(tmp_path, monkeypatch):
    """Fail-safe law: without the provider hub the law cannot tell an
    uninstall from an unknown — it acts NOT AT ALL (no park, no flag, no
    cleanup; the card file and its bytes stay exactly as they were)."""
    # hermetic hub-less: patch this file's copy AND the canonical one-copy
    # key the provider core resolves its seam modules through
    monkeypatch.setattr(DOCKING, "_plugins_memory", lambda: None)
    monkeypatch.setitem(sys.modules, "memrack_docking_core", DOCKING)
    _dock_home(tmp_path, "fakesvc")
    prov = _init(tmp_path)
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert (dock / "fakesvc.yaml").exists(), "no hub = no law action"
    card = yaml.safe_load((dock / "fakesvc.yaml").read_text(encoding="utf-8"))
    assert "uninstalled_at" not in card, card
    report = prov._uninstalled_report
    assert not (report["parked"] or report["removed"] or
                report["restored"]), report


def test_init_grace_report_is_honest_and_idempotent(tmp_path, monkeypatch):
    """The initialize-time report is honest (parked entries named) and the
    law is CHURN-FREE: a second initialize over the same home re-folds to
    the same state without rewriting the parked card file (no hidden churn)."""
    _dock_home(tmp_path, "fakesvc")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    prov = _init(tmp_path)
    report = prov._uninstalled_report
    assert report["retention_days"] == 14, report        # the default stand
    assert [p["provider"] for p in report["parked"]] == ["fakesvc"], report
    report_kept = report["removed"]
    assert report_kept == [], report_kept               # nothing expired yet

    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    parked_before = (dock / "disabled-fakesvc.yaml").read_bytes()
    _init(tmp_path)
    assert (dock / "disabled-fakesvc.yaml").read_bytes() == parked_before, \
        "the parked card is byte-identical across inits (idempotent law)"


# ── re-install before retention: restore + flag gone ───────────────────────

def test_reinstall_restores_card_and_drops_the_flag(tmp_path, monkeypatch):
    """Re-install before retention: the provider installed again → init
    restores the card (rename back, flags removed, NO re-probe — the card's
    recorded decisions stay) and the card racks by presence in the SAME
    initialize."""
    _dock_home(tmp_path, "fakesvc")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    _init(tmp_path)
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    card_before = yaml.safe_load((dock / "disabled-fakesvc.yaml").read_text(
        encoding="utf-8"))
    assert card_before["uninstalled_at"], card_before

    _fake_hub_path(monkeypatch, _hub(dirs=["fakesvc"]))   # installed again
    prov = _init(tmp_path)
    assert (dock / "fakesvc.yaml").exists(), "restored to the active name"
    assert not (dock / "disabled-fakesvc.yaml").exists()
    card = yaml.safe_load((dock / "fakesvc.yaml").read_text(encoding="utf-8"))
    assert "uninstalled_at" not in card, card
    assert "uninstall_note" not in card, card
    assert card["best_for"] == card_before["best_for"], \
        "restore without re-probe — the card text stays"
    status = _status(prov)
    assert "fakesvc" in status["backends"], status["backends"]
    assert status["uninstalled_docks"] == [], status["uninstalled_docks"]


# ── retention: the cleanup group ────────────────────────────────────────────

def _seed_parked_flagged(tmp_path, name, age_days, *, retention=None,
                         hub=None, monkeypatch=None):
    """Park + flagged card on file (simulated: the law ran, the clock aged)."""
    _dock_home(tmp_path, name)
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    (dock / ("%s.yaml" % name)).rename(dock / ("disabled-%s.yaml" % name))
    card = yaml.safe_load(
        (dock / ("disabled-%s.yaml" % name)).read_text(encoding="utf-8"))
    card["uninstalled_at"] = _flag_ts(age_days)
    (dock / ("disabled-%s.yaml" % name)).write_text(
        yaml.dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if monkeypatch is not None and hub is not None:
        _fake_hub_path(monkeypatch, hub)


def test_retention_removes_card_and_shim_with_backup(tmp_path, monkeypatch):
    """Retention law: an `uninstalled_at` older than the retention (default
    14) removes card + shim, with an honest per-file backup under
    plugin-data/memrack/removed-cards/ and the status note 'removed card
    <name> after retention'."""
    _seed_parked_flagged(tmp_path, "fakesvc", 15,
                         hub=_hub(uninstalled=["fakesvc"]),
                         monkeypatch=monkeypatch)
    prov = _init(tmp_path)
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert not (dock / "disabled-fakesvc.yaml").exists(), "card removed"
    assert not (dock / "fakesvc.py").exists(), "shim removed with its card"
    backups = tmp_path / "plugin-data" / "memrack" / "removed-cards"
    names = sorted(p.name for p in backups.iterdir())
    assert any(n.endswith("fakesvc.yaml") for n in names), names
    assert any(n.endswith("fakesvc.py") for n in names), names
    removed = prov._uninstalled_report["removed"]
    entry = next(r for r in removed if r["provider"] == "fakesvc")
    assert entry["note"] == "removed card fakesvc after retention", entry
    status = _status(prov)
    assert any(c["provider"] == "fakesvc"
               for c in status["retention_cleanups"]), status
    assert status["uninstalled_docks"] == [], \
        "after removal nothing stays parked: %s" % status["uninstalled_docks"]


def test_retention_zero_removes_immediately(tmp_path, monkeypatch):
    """`dock_retention_days: 0` = sofort: the park pass and the cleanup group
    run in the SAME initialize — the freshly flagged card does not survive
    it (with its backup)."""
    _dock_home(tmp_path, "fakesvc")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    prov = _init(tmp_path, {"dock_retention_days": 0})
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert not (dock / "disabled-fakesvc.yaml").exists(), "0 = immediate"
    assert not (dock / "fakesvc.py").exists(), "the shim leaves with the card"
    removed = prov._uninstalled_report["removed"]
    assert [r["provider"] for r in removed] == ["fakesvc"], removed


def test_retention_minus_one_never_removes(tmp_path, monkeypatch):
    """`dock_retention_days: -1` = never: an aged flag keeps the card parked
    (visible in status), no file is touched, no backup invented."""
    _seed_parked_flagged(tmp_path, "fakesvc", 100,
                         hub=_hub(uninstalled=["fakesvc"]),
                         monkeypatch=monkeypatch)
    prov = _init(tmp_path, {"dock_retention_days": -1})
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert (dock / "disabled-fakesvc.yaml").exists(), "-1 = never removes"
    assert prov._uninstalled_report["removed"] == []
    assert not (tmp_path / "plugin-data" / "memrack" /
                "removed-cards").exists(), "no backup invented"
    entry = next(e for e in _status(prov)["uninstalled_docks"]
                 if e["backend"] == "fakesvc")
    assert entry["days_left"] is None, entry   # indefinite = no countdown


def test_retention_garbage_falls_back_to_the_default(tmp_path, monkeypatch):
    """A non-integer `dock_retention_days` falls back to the DEFAULT 14 (the
    declared contract) — 10-day-old flags survive (10 < 14), and the parse
    never raises into the initialize."""
    _seed_parked_flagged(tmp_path, "fakesvc", 10,
                         hub=_hub(uninstalled=["fakesvc"]),
                         monkeypatch=monkeypatch)
    prov = _init(tmp_path, {"dock_retention_days": "garbage"})
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert (dock / "disabled-fakesvc.yaml").exists(), "10 < default 14"
    assert prov._uninstalled_report["removed"] == []


def test_corrupt_clock_never_removes(tmp_path, monkeypatch):
    """Fail-safe: an unparseable `uninstalled_at` must not expire the card —
    a corrupt clock never deletes anything (kept + honest)."""
    _dock_home(tmp_path, "fakesvc")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["fakesvc"]))
    _init(tmp_path)   # parks + flags
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    parked = dock / "disabled-fakesvc.yaml"
    card = yaml.safe_load(parked.read_text(encoding="utf-8"))
    card["uninstalled_at"] = "not-a-clock"
    parked.write_text(yaml.dump(card, allow_unicode=True, sort_keys=False),
                      encoding="utf-8")
    prov = _init(tmp_path)
    assert parked.exists(), "a corrupt clock never deletes"
    assert prov._uninstalled_report["removed"] == []


# ── `dock --refresh` applies the same grace ─────────────────────────────────

def test_dock_refresh_parks_an_uninstalled_provider(tmp_path, monkeypatch):
    """The explicit driver: `dock <provider> --refresh` on an uninstalled
    provider PARKS the active card (+ flag + honest report) instead of the
    bare refusal — and never deletes (no immediate file loss)."""
    _dock_home(tmp_path, "limbic")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["limbic"]))
    out = DOCKING.dock("limbic", refresh=True, hermes_home=str(tmp_path))
    assert out.get("status") == "uninstalled-parked", out
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert not (dock / "limbic.yaml").exists()
    parked = yaml.safe_load(
        (dock / "disabled-limbic.yaml").read_text(encoding="utf-8"))
    assert parked["uninstalled_at"], parked
    assert "uninstall_note" in parked, parked
    assert (dock / "limbic.py").exists(), "nothing deleted by the grace"


def test_dock_refresh_restores_a_reinstalled_flagged_card(tmp_path,
                                                          monkeypatch):
    """`dock <provider> --refresh` on a flagged PARKED card whose provider
    came back: restores (rename back + flags dropped) — a parked card
    WITHOUT the flag (the membership toggle's explicit park) is left alone."""
    _dock_home(tmp_path, "limbic")
    _fake_hub_path(monkeypatch, _hub(uninstalled=["limbic"]))
    _init(tmp_path)   # parks + flags
    # toggle-parked card (no flag) for the untouched law
    _dock_home(tmp_path, "togglesvc")
    CS.set_docked_membership("togglesvc", False, hermes_home=str(tmp_path))
    # the provider house changed: limbic AND togglesvc are installed again
    _fake_hub_path(monkeypatch, _hub(dirs=["limbic", "togglesvc"]))

    out = DOCKING.dock("limbic", refresh=True, hermes_home=str(tmp_path))
    assert out.get("status") == "restored-from-uninstall", out
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    assert (dock / "limbic.yaml").exists()
    card = yaml.safe_load((dock / "limbic.yaml").read_text(encoding="utf-8"))
    assert "uninstalled_at" not in card, card

    toggle_report = DOCKING.dock("togglesvc", refresh=True,
                                 hermes_home=str(tmp_path))
    assert toggle_report.get("status") == "parked-untouched", toggle_report
    assert (dock / "disabled-togglesvc.yaml").exists(), \
        "an explicit membership park is never undone by refresh"


# ── the config key: declared on both surfaces, dual-store conform ───────────

def test_retention_key_on_both_config_surfaces(tmp_path, monkeypatch):
    """`dock_retention_days` declared on the DECLARED pane (config_schema.py,
    default '14') AND the provider wizard surface (get_config_schema), and
    save_config writes it into BOTH stores (the dual-store law)."""
    cfg = MR._MemrackBase().get_config_schema()
    by_key = {f["key"]: f for f in cfg}
    field = by_key.get("dock_retention_days")
    assert field is not None, [f["key"] for f in cfg]
    assert field["default"] == 14, field

    home = tmp_path / "hermes-home"
    home.mkdir()
    prov = MR._MemrackBase()
    prov.save_config({"dock_retention_days": 14}, home)
    block = ((yaml.safe_load((home / "config.yaml").read_text())) or {}).get(
        "plugins", {}).get("memrack", {})
    import json
    flat = json.loads((home / "memrack" / "config.json").read_text(
        encoding="utf-8-sig"))
    assert block.get("dock_retention_days") == 14, block
    assert flat.get("dock_retention_days") == 14, flat


def test_declared_pane_carries_the_retention_field(monkeypatch):
    """The declared gear pane (config_schema.py) renders the retention field
    beside the static base fields — one NUMBER field, default 14, honest
    description."""
    _hermes = pathlib.Path.home() / ".hermes" / "hermes-agent"
    if _hermes.is_dir() and str(_hermes) not in sys.path:
        sys.path.insert(0, str(_hermes))
    pytest.importorskip(
        "plugins.memory.config_schema",
        reason="declared-surface rendering needs the Hermes runtime; "
               "fresh checkouts without the Hermes tree skip instead "
               "of failing")
    declared = _spec_load("_grace_declared_cfg_schema", "config_schema.py")
    kind = [f.kind for f in declared.CONFIG_SCHEMA.fields
            if f.key == "dock_retention_days"]
    assert kind == ["number"], kind
    field = next(f for f in declared.CONFIG_SCHEMA.fields
                 if f.key == "dock_retention_days")
    assert field.default == "14", field
    assert field.description, field
