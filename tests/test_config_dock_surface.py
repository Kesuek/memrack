"""T-036 contract tests — the config-surface + pending-confirmation flow,
GREEN after the scaffold stage pinned them (22 items, whole-file xfail then).

Source of truth: ~/.hermes/projects/memrack/references/catalog-rack-adapter-
design.md, section "## Addendum (2026-11-08): Config-Surface + Pending-
Confirmation-Flow" (points (1)-(4)) + the plan T-036 scope items (a)-(d) —
every test docstring cites the EXACT contract sentence it pins.

The tests run against the implemented seams:

    memrack/config_surface.py   (the six config-surface seams:
                                 membership_fields, merge_membership_toggle,
                                 set_docked_membership, dock_want_field,
                                 dock_want_hook, pending_flags, review_card)

The provider-core wiring (get_config_schema instance derivation, the
initialize() hook call, the status/cards-slot plumbing, the
``memrack_card_review`` tool surface incl. plugin.yaml, the declared
desktop surface) is pinned HERE and implemented in the GREEN phase.

SUITE LIFECYCLE: scaffold stage holds this whole file under
xfail(strict=False) — the scaffold report records the RED state honestly
(all items xfailed, 0 xpassed, baseline untouched). The GREEN phase
(coder, T-036) REMOVES the pytestmark and runs these contracts against the
implemented seams; the RED -> GREEN transition goes into the run report.

Ground rules: no network, no host plugin registry, no real catalog
providers — the docking-machine-level tests use the machine's OWN
loader/offers seams with in-process FakeProvider doubles (the same
pattern as tests/test_catalog_adapter.py); provider-level tests use
scratch homes and hand-injected rack members.
"""

import importlib.util
import json
import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _spec_load(mod_name, relpath):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CS = _spec_load("memrack_config_surface", "memrack/config_surface.py")
DOCKING = _spec_load("memrack_docking_t036", "memrack/docking.py")
PROXY = _spec_load("memrack_catalog_proxy_t036", "backends/_catalog_proxy.py")

# The memrack provider core (the wiring under test lives there — same
# spec-load pattern as tests/test_setup_declared.py / test_catalog_adapter.py).
MR = _spec_load("memrack", "__init__.py")
sys.modules["memrack"] = MR

# Suite lifecycle (the T-033 precedent completed): the scaffold stage held
# this whole file under xfail(strict=False) — RED documented, suite GREEN.
# The GREEN phase (coder, T-036) removed that marker: these contracts run
# against the implemented seams now; the RED -> GREEN transition is recorded
# in the run report (references/t036-config-dock-surface-2026-11-08.md) and
# .hermes/agent-runs/t036-config-dock-surface/.


class FakeProvider:
    """In-process duck double of the Hermes MemoryProvider ABC (hermes-less:
    no ABC import — the DOUBLED SURFACE is what the machine inspects)."""

    plugin_version = "9.9.9"

    def __init__(self, name="fakeprov"):
        self.name = name
        self.calls = []

    def is_available(self):
        return True

    def initialize(self, session_id="", **kwargs):
        self.calls.append("initialize")

    def shutdown(self):
        self.calls.append("shutdown")

    def get_tool_schemas(self):
        return [
            {"type": "function",
             "function": {"name": "fake_%d" % i, "description": "doubled tool %d" % i,
                          "parameters": {"type": "object", "properties": {}}}}
            for i in (1, 2, 3)
        ]

    def handle_tool_call(self, tool_name, args, **kwargs):
        self.calls.append("tool:%s" % tool_name)
        return '{"tool": "%s"}' % tool_name

    def system_prompt_block(self):
        return "FAKE-PROVIDER-BLOCK"

    def prefetch(self, query, *, session_id=""):
        return "FAKE-RECALL"

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        return None

    def on_memory_write(self, action, target, content, metadata=None, *,
                        session_id=""):
        self.calls.append("write")
        return {"status": "stored"}

    def on_session_end(self, messages, *, session_id=""):
        return None


def _loader_of(provider):
    """The machine's loader seam: () -> installed provider instance."""
    return lambda: provider


def _offer(provider_name, version="9.9.9"):
    """One catalog_offers entry (the machine's own offer descriptor shape)."""
    return {"name": provider_name, "source": "user", "version": version,
            "dir": None, "active": False}


def _draft_dock(tmp_path, provider_name="fakesvc"):
    """Seed ONE honest draft card through the REAL machine flow (its own
    GREEN machinery — no card invention here)."""
    fake = FakeProvider(provider_name)
    out = DOCKING.dock(provider_name, probe=True, loader=_loader_of(fake),
                       offers=[_offer(provider_name)], hermes_home=str(tmp_path))
    assert out.get("status") == "draft-docked", out
    return fake


def _docked_proxy(provider_name, draft=True, best_for=None, limits=None):
    """One docked rack member double with a manifest card (the shape
    test_catalog_adapter.py uses for the provider-level plumbing tests)."""
    fake = FakeProvider(provider_name)
    proxy = PROXY.CatalogProxyBackend(provider=fake, mirror_tools=True)
    manifest = {"name": provider_name, "module": provider_name + ".py",
                "search": [], "draft": draft,
                "mirror_tools": {"fake_1": "memrack__%s__fake_1" % provider_name}}
    if draft:
        manifest["best_for"] = "PLACEHOLDER — human-authored at the dock review"
        manifest["limits"] = "PLACEHOLDER — human-authored at the dock review"
    else:
        manifest["best_for"] = best_for or "humans wrote this after review"
        manifest["limits"] = limits or "no atomic write; read-only dock"
    proxy.manifest = manifest
    return proxy


class _SpyDocking:
    """Machine-module double that RECORDS the dock calls it received
    (the no-auto-dock pin: an absent/empty choice must never dock)."""

    def __init__(self):
        self.docked = []

    def catalog_offers(self, *args, **kwargs):
        return []

    def dock(self, provider_name, **kwargs):
        self.docked.append((provider_name, kwargs))
        return {"provider": provider_name, "status": "draft-docked", "spy": True}


# ── (a) dynamic membership toggles ──────────────────────────────────────────

def test_membership_fields_one_field_per_backend():
    """Addendum (1): 'get_config_schema() wird instanz-beziehbar — je
    Backend (built-ins + DOCKED aus backends.d) ein bool-Feld
    <name>_in_rack.' — exactly ONE toggle per backend name, in the wizard
    bool shape, and nothing else leaks into the dynamic field set."""
    fields = CS.membership_fields(
        {"enabled_backends": "base-db,state-episodic"},
        ["base-db", "state-episodic", "wiki"], ["honcho"])
    keys = [f["key"] for f in fields]
    assert len(keys) == len(set(keys)) == 4, fields
    assert "honcho_in_rack" in keys, fields          # the clean-name literal form
    for f in fields:
        assert f["key"].endswith("_in_rack"), f
        assert set(f["choices"]) == {"true", "false"}, f
    assert not {"backend", "enabled_backends", "auto_enable",
                "search_top_k"} & set(keys), fields


def test_membership_defaults_match_the_current_stand():
    """Addendum (1): '... (default passend zum aktuellen membership-Stand)':
    a built-in inside enabled_backends renders a truthy default, a parked
    built-in a falsy one; a docked card racks by presence (the registry
    law), so its toggle default is truthy."""
    fields = CS.membership_fields(
        {"enabled_backends": "base-db,state-episodic"},
        ["base-db", "state-episodic", "wiki"], ["honcho"])
    defaults = {f["key"]: f["default"] for f in fields}
    assert MR._truthy(defaults["base-db_in_rack"]) is True          # floor, enabled
    assert MR._truthy(defaults["state-episodic_in_rack"]) is True   # enabled
    assert MR._truthy(defaults["wiki_in_rack"]) is False            # parked by default
    assert MR._truthy(defaults["honcho_in_rack"]) is True           # docked = present


def test_membership_field_order_is_restart_stable():
    """Plan T-036 (coder duty): the dynamic field list is 'stabil sortiert'
    — the same inputs render the SAME field order twice (restart-stable
    ordering, no nondeterministic container order)."""
    one = CS.membership_fields({"enabled_backends": "base-db"},
                               ["wiki", "honcho", "base-db"], ["qdrant"])
    two = CS.membership_fields({"enabled_backends": "base-db"},
                               ["wiki", "honcho", "base-db"], ["qdrant"])
    assert one == two and len(one) == 4, (one, two)


def test_built_in_toggle_fold_is_consistent_and_keeps_laws():
    """Addendum (1): 'Toggle schreibt die Str-Felder konsistent;
    Basisfall-Gesetze bleiben' + plan scope 1: 'Schreibpfad = konsistenter
    Update von enabled_backends/docks (dedupe; base-db nie komplett weg,
    wiki geparkt-default)' — the fold dedupes, keeps every other key and
    the floor anchored, and never flips unrelated parked members in."""
    cfg = {"enabled_backends": "base-db,state-episodic", "search_top_k": 8}
    out = CS.merge_membership_toggle(cfg, "honcho_in_rack", "true")
    assert out is not cfg, "the fold returns a NEW config, never mutates"
    assert MR._norm_backends(out["enabled_backends"]) == \
        ["base-db", "state-episodic", "honcho"], out
    assert out["enabled_backends"].count("honcho") == 1, out   # dedupe
    assert out["search_top_k"] == 8, out                       # other keys ride along

    off = CS.merge_membership_toggle(cfg, "state-episodic_in_rack", False)
    assert MR._norm_backends(off["enabled_backends"]) == ["base-db"], off

    floor = CS.merge_membership_toggle(cfg, "base-db_in_rack", "false")
    assert "base-db" in MR._norm_backends(floor["enabled_backends"]), \
        "Basisfall: the floor is never toggled completely off"

    wiki_stays = CS.merge_membership_toggle(cfg, "honcho_in_rack", "true")
    assert "wiki" not in MR._norm_backends(wiki_stays["enabled_backends"]), \
        "Basisfall: wiki stays parked-by-default (no silent opt-in)"


def test_docked_toggle_parks_and_unparks_the_card(tmp_path):
    """Plan scope 1 ('... Update von enabled_backends/docks') + the docking
    law's established 'disabled-<name>.yaml' parking convention: toggling a
    DOCKED member off parks its card file (presence semantics, no
    deletion), toggling on restores it; unknown card name = controlled
    error; the shim byte stream is never touched."""
    _draft_dock(tmp_path, "fakesvc")
    dock_dir = tmp_path / "plugins" / "memrack" / "backends.d"
    card_path = dock_dir / "fakesvc.yaml"
    shim_path = dock_dir / "fakesvc.py"
    shim_before = shim_path.read_text(encoding="utf-8")
    card_before = yaml.safe_load(card_path.read_text(encoding="utf-8"))

    out = CS.set_docked_membership("fakesvc", False, hermes_home=str(tmp_path))
    assert "error" not in out, out
    assert not card_path.exists(), "toggled off = the active card is parked"
    parked = dock_dir / "disabled-fakesvc.yaml"
    assert parked.exists(), out
    assert yaml.safe_load(parked.read_text(encoding="utf-8"))["best_for"] == \
        card_before["best_for"], "the card text survives the park"
    assert shim_path.read_text(encoding="utf-8") == shim_before, \
        "card edits never touch the shim"

    back = CS.set_docked_membership("fakesvc", True, hermes_home=str(tmp_path))
    assert "error" not in back, back
    assert card_path.exists() and not parked.exists(), back
    assert yaml.safe_load(card_path.read_text(encoding="utf-8")) == card_before

    nobody = CS.set_docked_membership("nobody", False, hermes_home=str(tmp_path))
    assert "error" in nobody, "unknown card name = controlled error"
    assert not (dock_dir / "nobody.yaml").exists(), "nothing invented/written"
    assert not (dock_dir / "disabled-nobody.yaml").exists(), nobody


def _plugins_block(tmp_path):
    """plugins.memrack from the wizard store (the write-path's yaml leg)."""
    import yaml
    data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8-sig"))
    return ((data or {}).get("plugins") or {}).get("memrack") or {}


def test_toggle_fold_writes_the_stores_consistently(tmp_path):
    """Addendum (1): 'Toggle schreibt die Str-Felder konsistent' — the
    write-path fold runs against the CURRENT live stand (a partial submit
    carrying only the toggle folds onto the rack's stand instead of
    resetting enabled_backends to defaults), the folded stand is written
    once, and a persisted toggle key echoes the folded stand (a
    floor-absorbed off re-renders 'true' — the Basisfall law visible in the
    UI); a DOCKED member's toggle never persists a key (presence is the
    state)."""
    prov = MR._MemrackBase()
    prov.save_config({"wiki_in_rack": True}, str(tmp_path))
    block = _plugins_block(tmp_path)
    assert MR._norm_backends(block["enabled_backends"]) == \
        ["base-db", "state-episodic", "wiki"], block
    assert block["wiki_in_rack"] == "true", block

    prov.save_config({"base-db_in_rack": "false"}, str(tmp_path))
    block = _plugins_block(tmp_path)
    assert "base-db" in MR._norm_backends(block["enabled_backends"]), \
        "Basisfall: the floor is never toggled completely off (store form)"
    assert block["base-db_in_rack"] == "true", \
        "the stored key echoes the folded stand, never the absorbed lie: %s" % block

    prov.save_config({"state-episodic_in_rack": "false"}, str(tmp_path))
    block = _plugins_block(tmp_path)
    assert MR._norm_backends(block["enabled_backends"]) == ["base-db", "wiki"], block
    assert block["state-episodic_in_rack"] == "false", block
    assert block["wiki_in_rack"] == "true", block   # only the toggled name moved


def test_get_config_schema_is_instance_derived(tmp_path):
    """Addendum (1): 'get_config_schema() wird instanz-beziehbar — je
    Backend (built-ins + DOCKED aus backends.d)' — the initialized
    instance's schema carries a toggle per known backend (rack members,
    parked built-ins, the docked card) AND keeps the static fields
    (additive-safe: the existing wizard surface stays)."""
    rack = MR._MemrackBase(config={"enabled_backends": "base-db"})
    rack.initialize(session_id="CS", hermes_home=str(tmp_path))
    rack._rack["fakesvc"] = _docked_proxy("fakesvc", draft=True)
    fields = rack.get_config_schema()
    by_key = {f["key"]: f for f in fields}
    assert {"backend", "enabled_backends", "auto_enable"} <= set(by_key), \
        "the static wizard surface stays (additive-safe)"
    assert MR._truthy(by_key["base-db_in_rack"]["default"]) is True
    assert MR._truthy(by_key["state-episodic_in_rack"]["default"]) is False  # parked
    assert MR._truthy(by_key["wiki_in_rack"]["default"]) is False            # parked
    assert MR._truthy(by_key["fakesvc_in_rack"]["default"]) is True          # docked


# ── (b) the dock_want select + the initialize hook ──────────────────────────

def test_dock_want_field_choices_offers_plus_none():
    """Addendum (2): 'dock_want (select): choices = offers + \"\" (none)'
    — every offer name and the none choice, deterministic order."""
    field = CS.dock_want_field([_offer("honcho"), _offer("qdrant-memory")])
    assert field["key"] == "dock_want", field
    assert field["choices"].count("") == 1, field
    assert set(field["choices"]) == {"", "honcho", "qdrant-memory"}, field


def test_dock_want_choice_runs_the_existing_machine(tmp_path):
    """Addendum (2): 'Beim nächsten initialize(): dock_want gesetzt →
    probe+draft-card+shim via die EXISTIERENDE Docking-Maschine' — the hook
    drives the REAL machine (its dock(probe=True) flow), producing the
    machine's own artifacts: the draft card with PROPOSED placeholders and
    the binding shim."""
    fake = FakeProvider("fakeprov")
    out = CS.dock_want_hook(
        {"dock_want": "fakeprov"}, hermes_home=str(tmp_path), docking=DOCKING,
        offers=[_offer("fakeprov")], loader=_loader_of(fake))
    assert out.get("status") == "draft-docked", out
    dock_dir = tmp_path / "plugins" / "memrack" / "backends.d"
    card_file = dock_dir / "fakeprov.yaml"
    assert card_file.exists(), out
    assert (dock_dir / "fakeprov.py").exists(), "the shim is part of the flow"
    card = yaml.safe_load(card_file.read_text(encoding="utf-8"))
    assert card["draft"] is True, card
    assert card["best_for"].startswith("PLACEHOLDER"), card


def test_dock_want_without_choice_docks_nothing(tmp_path):
    """Addendum (2): '(die Konfig-Wahl IST die User-Entscheidung — kein
    heimliches Auto-Dock)' — an absent or empty dock_want NEVER calls the
    machine: the hook is a no-op (None) and no card appears."""
    spy = _SpyDocking()
    assert CS.dock_want_hook({}, hermes_home=str(tmp_path),
                             docking=spy) is None
    assert CS.dock_want_hook({"dock_want": ""}, hermes_home=str(tmp_path),
                             docking=spy) is None
    assert spy.docked == [], "no choice, no machine call, no auto-dock"


def test_dock_want_unknown_choice_is_honest_refusal(tmp_path):
    """Addendum (2) + the machine's fail-closed law: a choice that is not an
    installed, inactive offer is handed to the machine and its REFUSAL is
    returned honestly — no invented dock, no card written."""
    out = CS.dock_want_hook(
        {"dock_want": "nobody"}, hermes_home=str(tmp_path), docking=DOCKING,
        offers=[], loader=_loader_of(FakeProvider("nobody")))
    assert out.get("refused") is True, out
    assert not (tmp_path / "plugins" / "memrack" / "backends.d" /
                "nobody.yaml").exists(), "a refused dock never leaves a card"


def test_dock_want_choice_over_an_explicit_park_refuses(tmp_path):
    """Addendum (2) + the membership-toggle law (the same fail-closed family
    as the machine's reviewed-card law): a persisted dock_want whose dock
    card is PARKED (the toggle's later, explicit decision) does not re-dock
    — the honest refusal comes back, the machine is never consulted and the
    card file stays parked (a stale select must not churn over an explicit
    park)."""
    _draft_dock(tmp_path, "fakesvc")
    assert "error" not in CS.set_docked_membership(
        "fakesvc", False, hermes_home=str(tmp_path))
    spy = _SpyDocking()
    out = CS.dock_want_hook({"dock_want": "fakesvc"},
                            hermes_home=str(tmp_path), docking=spy)
    assert out.get("refused") is True, out
    assert spy.docked == [], "a parked dock is never re-probed by a stale select"
    dock_dir = tmp_path / "plugins" / "memrack" / "backends.d"
    assert not (dock_dir / "fakesvc.yaml").exists(), out
    assert (dock_dir / "disabled-fakesvc.yaml").exists(), "the park stands"


def test_get_config_schema_carries_the_dock_want_select(tmp_path):
    """Addendum (2): the provider's instance schema surfaces the dock_want
    select field (the UI choice needs the field on the same surface that
    carries the toggles)."""
    rack = MR._MemrackBase(config={"enabled_backends": "base-db"})
    rack.initialize(session_id="CS", hermes_home=str(tmp_path))
    fields = rack.get_config_schema()
    by_key = {f["key"]: f for f in fields}
    assert "dock_want" in by_key, fields
    assert "" in by_key["dock_want"]["choices"], by_key["dock_want"]


def test_initialize_wires_the_dock_want_hook(tmp_path):
    """Addendum (2): 'Beim nächsten initialize(): dock_want gesetzt → ...'
    — initialize() is the consumption point: it hands the provider's
    merged config (the choice) and its hermes_home to the config-surface
    hook seam."""
    recorded = {}

    def spy_hook(config, hermes_home=None, **kwargs):
        recorded["config"] = dict(config)
        recorded["hermes_home"] = hermes_home
        return None

    # The core resolves the config-surface module under the canonical
    # sys.modules name (one copy per process; the core's spec-load registers
    # its own copy absent ours — its __file__-equality attribute patch on
    # THIS module object stays invisible to it otherwise). The registration
    # is the wiring-visible form of the scaffold's attribute spy (the
    # scaffold LOG §6 pre-authorised this honest contract revision; the
    # pinned contract = initialize() IS the consumption point handing the
    # merged config + hermes_home — unchanged).
    original_cs = sys.modules.get("memrack_config_surface")
    sys.modules["memrack_config_surface"] = CS
    original = CS.dock_want_hook
    CS.dock_want_hook = spy_hook
    try:
        rack = MR._MemrackBase(
            config={"dock_want": "fakeprov", "enabled_backends": "base-db"})
        rack.initialize(session_id="DW", hermes_home=str(tmp_path))
    finally:
        CS.dock_want_hook = original
        if original_cs is None:
            sys.modules.pop("memrack_config_surface", None)
        else:
            sys.modules["memrack_config_surface"] = original_cs
    assert recorded, "initialize() must call the dock_want hook"
    assert recorded["config"].get("dock_want") == "fakeprov", recorded
    assert recorded["hermes_home"].endswith(tmp_path.name), recorded


# ── (c) pending-confirmation plumbing in status + every tool answer ─────────

def test_pending_flags_shape_unit():
    """Addendum (3): 'Cards mit PROPOSED-best_for/limits (draft:true) tragen
    ... pending_confirmation: true + pending_hint' (unit shape) — the draft
    card yields BOTH flags, a reviewed card yields none."""
    draft = _docked_proxy("fakesvc", draft=True)
    flags = CS.pending_flags(dict(draft.manifest))
    assert flags.get("pending_confirmation") is True, flags
    assert isinstance(flags.get("pending_hint"), str) and \
        flags["pending_hint"].strip(), flags
    reviewed = {"name": "fakesvc", "best_for": "humans wrote this after review",
                "limits": "no atomic write; read-only dock"}
    assert CS.pending_flags(reviewed) == {}, "reviewed cards never pend"


def test_draft_card_flags_surface_in_status_cards(tmp_path):
    """Addendum (3): '... tragen in memrack_status und im cards-Slot ...'
    — the status card of a draft dock carries pending_confirmation +
    pending_hint; non-draft rack members stay clean."""
    rack = MR._MemrackBase(config={"enabled_backends": "base-db"})
    rack.initialize(session_id="PD", hermes_home=str(tmp_path))
    rack._rack["fakesvc"] = _docked_proxy("fakesvc", draft=True)
    status = json.loads(rack.handle_tool_call("memrack_status", {}))
    card = status["cards"]["fakesvc"]
    assert card.get("pending_confirmation") is True, card
    assert isinstance(card.get("pending_hint"), str) and \
        card["pending_hint"].strip(), card
    assert "pending_confirmation" not in status["cards"]["base-db"], \
        "only PROPOSED (draft) cards pend"


def test_draft_card_flags_surface_on_every_tool_answer(tmp_path):
    """Addendum (3): 'Meldung an den Agenten auf JEDER Tool-Antwort' — the
    cards slot of memrack_search AND memrack_write answers carries the
    flags on a draft dock."""
    rack = MR._MemrackBase(config={"enabled_backends": "base-db"})
    rack.initialize(session_id="PD", hermes_home=str(tmp_path))
    rack._rack["fakesvc"] = _docked_proxy("fakesvc", draft=True)
    for tool, args in (("memrack_search", {"query": "anything"}),
                       ("memrack_write", {"content": "one atomic fact"})):
        out = json.loads(rack.handle_tool_call(tool, args))
        card = out["cards"]["fakesvc"]
        assert card.get("pending_confirmation") is True, (tool, card)
        assert card.get("pending_hint", "").strip(), (tool, card)


def test_reviewed_card_carries_no_pending_flags(tmp_path):
    """Addendum (3): only PROPOSED cards pend — 'Cards mit PROPOSED-
    best_for/limits (draft:true) tragen ...' — a dock whose review ran
    (draft gone, final best_for/limits) surfaces NO pending keys in the
    status card nor in the tool answers' cards slot, while the DRAFT
    sibling in the same rack still pends (no blanket suppression — the
    complementarity stays provable)."""
    rack = MR._MemrackBase(config={"enabled_backends": "base-db"})
    rack.initialize(session_id="PD", hermes_home=str(tmp_path))
    rack._rack["draftdock"] = _docked_proxy("draftdock", draft=True)
    rack._rack["fakesvc"] = _docked_proxy(
        "fakesvc", draft=False, best_for="humans wrote this after review",
        limits="no atomic write; read-only dock")
    status = json.loads(rack.handle_tool_call("memrack_status", {}))
    assert status["cards"]["draftdock"].get("pending_confirmation") is True, \
        "the draft sibling still pends (complementarity)"
    assert "pending_confirmation" not in status["cards"]["fakesvc"], status
    assert "pending_hint" not in status["cards"]["fakesvc"], status
    out = json.loads(rack.handle_tool_call(
        "memrack_search", {"query": "anything"}))
    assert not {"pending_confirmation", "pending_hint"} & \
        set(out["cards"]["fakesvc"]), out
    out_w = json.loads(rack.handle_tool_call(
        "memrack_write", {"content": "one atomic fact"}))
    assert not {"pending_confirmation", "pending_hint"} & \
        set(out_w["cards"]["fakesvc"]), out_w


# ── (d) the memrack_card_review tool ────────────────────────────────────────

def test_review_card_sets_reviewed_text_and_clears_draft(tmp_path):
    """Addendum (4): 'setzt reviewte Texte, draft→reviewed' — the human
    texts land VERBATIM, the draft marker clears and the shim stays byte-
    identical (card edits never need a shim rewrite)."""
    _draft_dock(tmp_path, "fakesvc")
    dock_dir = tmp_path / "plugins" / "memrack" / "backends.d"
    card_path = dock_dir / "fakesvc.yaml"
    shim_path = dock_dir / "fakesvc.py"
    shim_before = shim_path.read_text(encoding="utf-8")

    out = CS.review_card("fakesvc", "humans wrote this after review",
                         "no atomic write; read-only dock",
                         hermes_home=str(tmp_path))
    assert "error" not in out, out
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    assert card["best_for"] == "humans wrote this after review", card
    assert card["limits"] == "no atomic write; read-only dock", card
    assert "draft" not in card, card
    assert shim_path.read_text(encoding="utf-8") == shim_before, out


def test_review_card_appends_chat_confirm_revision(tmp_path):
    """Addendum (4): '... revisions-Entry (timestamp, source=chat-confirm)'
    — exactly ONE revision entry with the Addendum's key names."""
    _draft_dock(tmp_path, "fakesvc")
    out = CS.review_card("fakesvc", "humans wrote this after review",
                         "no atomic write; read-only dock",
                         hermes_home=str(tmp_path))
    assert "error" not in out, out
    card_path = tmp_path / "plugins" / "memrack" / "backends.d" / "fakesvc.yaml"
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    revisions = card["revisions"]
    assert len(revisions) == 1, revisions
    entry = revisions[-1]
    assert entry["source"] == "chat-confirm", entry
    assert isinstance(entry["timestamp"], str) and entry["timestamp"].strip(), entry


def test_review_card_unknown_backend_controlled_error(tmp_path):
    """Addendum (4): 'Unbekanntes Backend ... = controlled error' — an
    honest {"error": ...} report, nothing written, no card invented."""
    out = CS.review_card("nobody", "a text", "a limit", hermes_home=str(tmp_path))
    assert "error" in out, out
    assert not (tmp_path / "plugins" / "memrack" / "backends.d" /
                "nobody.yaml").exists(), "a refused review never writes"


def test_review_card_empty_text_controlled_error(tmp_path):
    """Addendum (4): '... leere (whitespace-only) Texte = controlled error'
    — either field empty refuses the review; the card file stays exactly
    as it was (fail-closed, no partial write)."""
    _draft_dock(tmp_path, "fakesvc")
    card_path = tmp_path / "plugins" / "memrack" / "backends.d" / "fakesvc.yaml"
    raw_before = card_path.read_text(encoding="utf-8")
    out = CS.review_card("fakesvc", "", "a limit", hermes_home=str(tmp_path))
    assert "error" in out, out
    out2 = CS.review_card("fakesvc", "a text", "   ", hermes_home=str(tmp_path))
    assert "error" in out2, out2
    assert card_path.read_text(encoding="utf-8") == raw_before, out


def test_reviewed_card_survives_drift_recheck(tmp_path):
    """Addendum (4): 'Reviewed Cards sind Drift-Recheck-immun (existierende
    Law)' — after a review, the machine's drift recheck (same version:
    no churn by design) leaves the human card text untouched."""
    _draft_dock(tmp_path, "fakesvc")
    out = CS.review_card("fakesvc", "humans wrote this after review",
                         "no atomic write; read-only dock",
                         hermes_home=str(tmp_path))
    assert "error" not in out, out
    recheck = DOCKING.drift_recheck(
        "fakesvc", loader=_loader_of(FakeProvider("fakesvc")),
        hermes_home=str(tmp_path))
    assert recheck.get("drifted") is False, recheck
    card = yaml.safe_load(
        (tmp_path / "plugins" / "memrack" / "backends.d" / "fakesvc.yaml")
        .read_text(encoding="utf-8"))
    assert card["best_for"] == "humans wrote this after review", card
    assert "draft" not in card, card


def test_card_review_tool_surface(tmp_path):
    """Addendum (4) + plan (plugin.yaml surface update): the tool surface
    carries memrack_card_review (args backend/best_for/limits) and the
    dispatch runs the review writer through the provider core — an
    unknown backend answers the controlled error, never a crash."""
    rack = MR._MemrackBase(config={"enabled_backends": "base-db"})
    rack.initialize(session_id="RV", hermes_home=str(tmp_path))
    schemas = rack.get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "memrack_card_review" in names, names
    schema = next(s for s in schemas
                  if s["function"]["name"] == "memrack_card_review")
    assert {"backend", "best_for", "limits"} <= \
        set(schema["function"]["parameters"]["properties"]), schema
    out = json.loads(rack.handle_tool_call(
        "memrack_card_review",
        {"backend": "nobody", "best_for": "a text", "limits": "a limit"}))
    assert "error" in out, out
