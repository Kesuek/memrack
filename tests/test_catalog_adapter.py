"""T-033 RED tests — the catalog-rack-adapter contract, pinned BEFORE the
implementation exists (scaffold stage).

Source of truth: ~/.hermes/projects/memrack/references/catalog-rack-adapter-design.md
(cited per test, in its own docstring) + plan .hermes/plans/t033-adapter-impl.md.

Until GREEN the tests run against honest NotImplementedError stubs:

    memrack/namechain.py         (the 64-cap name chain)
    memrack/docking.py           (docking machine + probe runner)
    backends/_catalog_proxy.py   (CatalogProxyBackend, `_` = discovery-skip
                                  until the GREEN phase completes + renames)

SUITE LIFECYCLE: scaffold stage held this whole file under
xfail(strict=False) — the scaffold report recorded the RED state honestly
(23 xfailed, 0 failed, baseline untouched). The GREEN phase REMOVED that
marker and ran these contracts against the fresh implementations, plus
added the core-wiring section below (mirror dispatch, dock tool surface,
status-card names, write-forward channel, conformance mirror law) — the
RED -> GREEN transition is captured in the run report.

Fake doubles: in-process duck stubs of the Hermes MemoryProvider ABC —
NO network, NO honcho import, NO host plugin registry (plan § CODER);
every docking/probe call takes the loader/offers seams so the lazy
hermes-internal import paths are never touched hermes-less.
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


NAMECHAIN = _spec_load("memrack_namechain", "memrack/namechain.py")
PROXY = _spec_load("memrack_catalog_proxy", "backends/_catalog_proxy.py")
DOCKING = _spec_load("memrack_docking", "memrack/docking.py")
CONF = _spec_load("memrack_conformance", "memrack/conformance.py")

# The memrack plugin core (the T-031 provider-level fan-out lives here) —
# the fusion test proves the proxy enters the EXISTING single injection rail.
MR = _spec_load("memrack", "__init__.py")
sys.modules["memrack"] = MR   # same pattern as tests/test_sdk.py

_CAP = NAMECHAIN.NAME_CAP   # 64, per design ("64-Cap-Kette")


class FakeProvider:
    """In-process duck double of the Hermes MemoryProvider ABC (hermes-less:
    no ABC import — the doubled SURFACE is what the adapter mirrors)."""

    plugin_version = "9.9.9"          # doubles carry their version like a manifest would

    def __init__(self, name="fakeprov"):
        self.name = name
        self.calls = []
        self.initialize_args = None
        self.turns = []
        self.writes = []
        self.session_ends = []
        self.mirrored_calls = []

    # ABC core
    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.calls.append("initialize")
        self.initialize_args = (session_id, dict(kwargs))

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
        self.mirrored_calls.append(tool_name)
        return '{"tool": "%s"}' % tool_name

    # the bends the adapter mirrors
    def system_prompt_block(self):
        self.calls.append("spb")
        return "FAKE-PROVIDER-BLOCK"

    def prefetch(self, query, *, session_id=""):
        self.calls.append(("prefetch", query))
        return "FAKE-RECALL"

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        self.calls.append("sync_turn")
        self.turns.append((user_content, assistant_content, session_id))

    def on_memory_write(self, action, target, content, metadata=None):
        self.calls.append("write")
        self.writes.append((action, target, content, metadata))

    def on_session_end(self, messages):
        self.calls.append("session_end")
        self.session_ends.append(messages)


class FakePrefetchOnly(FakeProvider):
    """Sampling-bias double: NO tools, NO query surface — pure recall rail.
    The probe MUST classify it 'prefetch-only' (per-provider outcome)."""

    def get_tool_schemas(self):
        return []


class FakeSilent(FakeProvider):
    """A provider that publishes no static system-prompt block."""

    def system_prompt_block(self):
        self.calls.append("spb")
        return ""


class FakeToolcentric(FakeProvider):
    """Sampling-bias double: three (real-surface) tools + capture — the
    probe MUST NOT classify it 'prefetch-only'."""


class FakeBroken:
    """Double of a provider whose class cannot be instantiated (the probe
    refuses fail-closed; nothing is invented for a broken dock)."""

    name = "brokenprov"

    def __init__(self, *a, **k):
        raise RuntimeError("provider class exploded in isolation")


def _loader_of(fake):
    def _loader():
        return fake
    return _loader


def _offer(name, home):
    """An offer descriptor like the real catalog scan would deliver."""
    return {"name": name, "source": "user", "version": "9.9.9",
            "dir": home, "active": False}


# ─────────────────────────────────────────────────────────────────────
# § Namensgesetz für gespiegelte Tools (64-Cap-Kette)
# ─────────────────────────────────────────────────────────────────────

def test_namechain_tier1_short_pair_keeps_full_prefix():
    """§ Namensgesetz: the base tier is `memrack__<provider>__<tool>` — a pair
    that fits stays in the plain memrack namespace; every effective name is
    bounded by the 64 cap."""
    eff = NAMECHAIN.mirror_tool_name("honcho", "honcho_search")
    assert eff == "memrack__honcho__honcho_search"
    assert len(eff) <= _CAP


def test_namechain_tier2_shrinks_prefix_over_cap():
    """§ Namensgesetz: 'bei >64 Zeichen: mr__<provider>__<tool>' — as soon as
    tier 1 exceeds the cap the shrink fires, AND the tier-2 form still carries
    provider AND tool fully when they fit."""
    provider, tool = "p" * 30, "t" * 25
    tier1_len = len("memrack__") + len(provider) + len("__") + len(tool)
    assert tier1_len > _CAP, "sanity: engineered pair must exceed the tier-1 cap"
    eff = NAMECHAIN.mirror_tool_name(provider, tool)
    assert eff.startswith("mr__"), eff
    assert provider in eff and tool in eff, eff          # tier 2 keeps both whole
    tier2_len = len("mr__") + len(provider) + len("__") + len(tool)
    assert tier2_len <= _CAP, "sanity: this pair fits the tier-2 budget"


def test_namechain_tier3_hash4_bounded_order_free():
    """§ Namensgesetz: still over the cap -> `mr__<p>__<t>__<hash4>`; the name
    is bounded by the cap, closes on a 4-char digest, and NEVER depends on the
    registration order (the digest derives from provider+tool)."""
    provider, tool = "p" * 40, "t" * 40
    eff = NAMECHAIN.mirror_tool_name(provider, tool)
    assert len(eff) <= _CAP, eff
    digest_tail = eff.rsplit("__", 1)[-1]
    assert len(digest_tail) == 4, "design names the tier-3 tail hash4: %r" % eff
    # NIE Registrierungs-Reihenfolge: the same pair mirrored in any set order
    # yields the identical name.
    before = NAMECHAIN.mirror_tool_names(provider, [tool, "small"])
    after = NAMECHAIN.mirror_tool_names(provider, ["small", tool])
    assert before[tool] == after[tool] == eff


def test_namechain_deterministic_across_restarts():
    """§ Namensgesetz: 'Alias muss über Restarts stabil bleiben, sonst
    Prompt-Cache-Breaks' — two independent calls (in production: fresh module
    loads each session) give byte-identical names."""
    first = NAMECHAIN.mirror_tool_name("honcho", "honcho_search")
    second = NAMECHAIN.mirror_tool_name("honcho", "honcho_search")
    assert first == second and second == "memrack__honcho__honcho_search"


def test_namechain_uniqueness_law_fails_closed_on_duplicate():
    """§ Namensgesetz: 'Kollisionen -> Conformance bricht mit Fehler ab
    (fail-closed)' — a mirror registration carrying two tools under ONE
    effective name aborts with MirrorNameError; nothing silently dedupes."""
    with pytest.raises(NAMECHAIN.MirrorNameError):
        NAMECHAIN.ensure_unique(["mr__same__name", "mr__same__name"],
                                origin="fakeprov tools 1+2")


def test_namechain_uniqueness_law_accepts_disjoint_set():
    """§ Namensgesetz (positive half): a collision-free mirror set is NOT
    refused — the law gates, it never invents refusals."""
    result = NAMECHAIN.ensure_unique(
        ["mr__a", "mr__b", "memrack__honcho__q"], origin="fakeprov")
    assert result in (True, None)


# ─────────────────────────────────────────────────────────────────────
# CatalogProxyBackend — Proxy-Design table, mirror-not-rebuild
# ─────────────────────────────────────────────────────────────────────

def test_proxy_lifecycle_pass_through_keeps_provider_own_state(tmp_path):
    """Proxy table, rows initialize/shutdown/export: 'Pass-through (eigener
    Store/Deps/Config verbleibt beim Provider)' — initialize forwards the
    session binding (the ABC multiplexing contract: fresh instance per load),
    shutdown flushes the PROVIDER, export stays card-shaped and provider-named."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake)
    proxy.initialize(session_id="S1", hermes_home=str(tmp_path), provider_config={})
    assert fake.initialize_args[0] == "S1"
    assert "initialize" in fake.calls
    proxy.shutdown()
    assert "shutdown" in fake.calls
    ex = proxy.export()
    assert isinstance(ex, dict) and ex.get("name") == "fakesvc"


def test_proxy_turn_ingestion_pass_through():
    """Proxy table, row on_turn_complete/sync_turn: 'Pass-through (deren
    Ingestion)' — the rack's backend on_turn reaches the provider's sync_turn
    with user/assistant/session intact (their ingestion, not a copy)."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake)
    proxy.on_turn("user said hi", "assistant replied", session_id="sid7")
    assert fake.turns == [("user said hi", "assistant replied", "sid7")]


def test_proxy_session_end_pass_through():
    """Proxy table, rest pass-through: on_session_end reaches the provider's
    hook with the message list intact (capture stays theirs, mirrored only)."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake)
    proxy.on_session_end([{"role": "user", "content": "x"}], session_id="s")
    assert fake.session_ends == [[{"role": "user", "content": "x"}]]


def test_proxy_system_prompt_block_concat_with_origin_line():
    """Umlenkung ②: 'system_prompt_block() -> Concat in memracks Block mit
    Herkunfts-Headerzeile' — the provider's static block surfaces INSIDE the
    proxy block behind an origin line naming the provider; and NOTHING is
    emitted for a silent provider (no phantom origin headers)."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake)
    out = proxy.system_prompt_block()
    assert "FAKE-PROVIDER-BLOCK" in out
    assert "fakesvc" in out.splitlines()[0], "origin line first: %r" % out

    out2 = PROXY.CatalogProxyBackend(provider=FakeSilent("silent")).system_prompt_block()
    assert out2 == "", "a silent provider must not get an origin line for nothing"


def test_proxy_prefetch_enters_the_single_injection_rail(tmp_path):
    """Umlenkung ①: 'prefetch(query, sid): Ergebnis NICHT direkt an den Host —
    memrack fängt es und fusioniert es in EINEN eigenen prefetch-Block.' The
    proxy serves its text THROUGH the T-031 provider-level fan-out (one
    injection schiene): after docking the proxy into a real memrack rack, the
    rack-level prefetch carries the provider text AND the static block arrives
    via the proxy — recall never leaks into the static block."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake)
    rack = MR._MemrackBase(config={})
    rack.initialize(session_id="FUSE", hermes_home=str(tmp_path))
    rack._rack["fakesvc"] = proxy
    fused = rack.prefetch("q", session_id="FUSE")
    assert "FAKE-RECALL" in fused, "provider recall must be fused into the rack's ONE prefetch block"
    block = rack.system_prompt_block()
    assert "FAKE-PROVIDER-BLOCK" in block, "the docked provider's static block arrives via the proxy"
    assert "FAKE-RECALL" not in block, "recall goes to the prefetch rail, never the static block"


def test_proxy_without_card_claims_nothing():
    """Card honesty (SDK law: the card NEVER promises a dead mode): a docked
    proxy without reviewed card information claims NO modes and no card text —
    the card (probe outcome + human review), not the code, owns every claim;
    the placeholders stay honest-empty (conformance warns, no dummy copy)."""
    proxy = PROXY.CatalogProxyBackend(provider=FakePrefetchOnly("quiet2"))
    assert proxy.card_modes() == ()
    assert proxy.search_modes == ()
    assert proxy.best_for == "" and proxy.limits == ""
    assert proxy.writeable is False


def test_proxy_tool_mirror_namespace_and_off_decision():
    """Umlenkung ③ (hart): 'Tools unter memrack-Namensraum spiegeln
    (`memrack__<provider>__<tool>`) ODER bewusst nicht spiegeln — Entscheidung
    je Provider, in der Card dokumentiert.' With a recorded mirror decision the
    provider tools carry effective names; without a decision / with mirror off
    the mirror stays silent (fail-closed: mirroring without a decision is not
    mirroring)."""
    fake = FakeProvider("fakesvc")
    on = PROXY.CatalogProxyBackend(provider=fake, mirror_tools=True)
    schema_names = [s["function"]["name"] for s in on.tool_schemas()]
    assert len(schema_names) == 3
    assert "memrack__fakesvc__fake_1" in schema_names
    # the namespace law: every effective name opens with the MIRROR prefix —
    # tier 1 "memrack__<p>__<t>", the caps' shrink "mr__..." (the scaffold's
    # single "mr" prefix was contradictory: it excluded the tier-1 form the
    # test itself pins — fixed, law unchanged)
    for name in schema_names:
        assert len(name) <= _CAP and name.startswith(("memrack__", "mr__")), name

    undeclared = PROXY.CatalogProxyBackend(provider=fake)
    assert undeclared.tool_schemas() == [], "no decision on record -> no mirror"

    off = PROXY.CatalogProxyBackend(provider=fake, mirror_tools=False)
    assert off.tool_schemas() == [], "the recorded NOT-mirror decision is honest"


def test_proxy_mirror_dispatch_maps_back_to_original_names():
    """Umlenkung ③ + Namensgesetz: the HOST may only ever call the mirrored
    name; the proxy dispatches it to the provider with the ORIGINAL tool name
    ('der Provider spricht nie direkt mit dem Host'). An unmirrored (direct)
    tool name must fail closed — never a guessed tool."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake, mirror_tools=True)
    mirrored = "memrack__fakesvc__fake_2"
    answer = proxy.handle_mirrored_call(mirrored, {"k": 1})
    assert '"tool": "fake_2"' in str(answer)
    assert "tool:fake_2" in fake.calls
    with pytest.raises(KeyError):
        proxy.handle_mirrored_call("fake_2", {})   # the host must NOT bypass the mirror


def test_proxy_write_forward_is_a_per_provider_decision():
    """Proxy table, row on_memory_write: 'Forward — je Provider entschieden
    (nur wenn Write-Semantik kompatibel; sonst Card sagt „nur Retrieval")' —
    compatible: the write reaches the provider with action/target/content/
    metadata intact; incompatible or undecided: NEVER forwarded (the host's
    builtin-store writes stay builtin-owned; the None receipt = memrack's
    no-op)."""
    fake = FakeProvider("fakesvc")
    fwd = PROXY.CatalogProxyBackend(provider=fake, forward_writes=True)
    fwd.on_memory_write("add", "memory", "fact one", metadata={"write_origin": "memory"})
    assert fake.writes == [("add", "memory", "fact one", {"write_origin": "memory"})]

    shy = FakeProvider("fakesvc2")
    assert PROXY.CatalogProxyBackend(provider=shy, forward_writes=False).on_memory_write(
        "add", "memory", "fact two") is None
    assert PROXY.CatalogProxyBackend(provider=shy).on_memory_write(
        "add", "memory", "fact three") is None
    assert shy.writes == [], "undecided forwarding must not write into the provider"


def test_proxy_prefetch_only_claims_no_search_modes():
    """§ Sampling-Bias-Law + proxy table row Retrieval/Query-API: 'wenn nur
    prefetch -> Kontext-Engine-Kategorie, nur über Kanal ①, Card sagt es
    ehrlich.' A prefetch-only dock claims NO rack search modes — the card
    (probe outcome), not the code, owns the claim. A query-capable decision is
    the OTHER honest state (probes decide per provider)."""
    proxy_only = PROXY.CatalogProxyBackend(provider=FakePrefetchOnly("quiet"))
    proxy_only.manifest = {"name": "quiet", "provider": "quiet", "draft": True,
                           "search": [], "mirror_tools": {}}
    assert proxy_only.card_modes() == (), "prefetch-only: no rack search modes claimed"
    assert proxy_only.search_modes == ()
    proxy_qcap = PROXY.CatalogProxyBackend(provider=FakeProvider("talky"), query_capable=True)
    assert callable(getattr(proxy_qcap, "search", None))


# ─────────────────────────────────────────────────────────────────────
# Docking machine — probe, cards, offers, drift
# ─────────────────────────────────────────────────────────────────────

def test_probe_classifies_per_provider(tmp_path):
    """§ Sampling-Bias-Law: 'Probe-Ergebnis je Provider IST die Wahrheit …
    prefetch-only vs. query-capable ist ein Probe-Outcome, nie ein Prior' —
    a tool-centric and a prefetch-only double get DIFFERENT classifications,
    derived from each one's actual occupancy (never from our wiki sample)."""
    r_toolish = DOCKING.probe(
        "toolish", loader=_loader_of(FakeToolcentric("toolish")),
        hermes_home=str(tmp_path))
    r_quiet = DOCKING.probe(
        "quietprov", loader=_loader_of(FakePrefetchOnly("quietprov")),
        hermes_home=str(tmp_path))
    assert r_quiet["classification"] == "prefetch-only"
    assert r_toolish["classification"] != "prefetch-only"
    assert r_toolish["abc_occupied"] is True
    mirrored = [m["effective"] for m in r_toolish["mirror_tools"]]
    assert all(len(m) <= _CAP for m in mirrored)
    assert "evidence" in r_toolish and "probed_at" in r_toolish["evidence"], r_toolish


def test_probe_uninstalled_or_broken_refuses_fail_closed(tmp_path):
    """§ Card-Erstellung: 'Provider nicht installiert -> Docking verweigert
    (fail-closed, Gap-Note)' — a ghost (never installed) and a broken provider
    (explodes in isolation) both refuse: Gap-Note; NO card is written; nothing
    is invented for a dead dock."""
    def broken_loader():
        raise RuntimeError("provider class exploded in isolation")

    out_ghost = DOCKING.dock(
        "ghostprov", probe=True,
        offers=[_offer("fakesvc", str(tmp_path))],
        hermes_home=str(tmp_path))
    assert out_ghost.get("gap"), out_ghost

    out_broken = DOCKING.dock(
        "fakesvc", probe=True, loader=broken_loader,
        offers=[_offer("fakesvc", str(tmp_path))],
        hermes_home=str(tmp_path))
    assert out_broken.get("gap"), out_broken

    for name in ("ghostprov", "fakesvc"):
        assert not (tmp_path / "plugins" / "memrack" / "backends.d" /
                    ("%s.yaml" % name)).exists(), "a refused dock never leaves a card"


def test_dock_probe_writes_draft_card_with_placeholders(tmp_path):
    """§ Card-Erstellung + plan scope 2: 'memrack dock <provider> --probe …
    schreibt ENTWURFS-Karte (search/Modi abgeleitet + Tool-Mirror-Liste) +
    drift-recheck-Evidence (timestamp + provider-version) in
    backends.d/<provider>.yaml' — and the DRAFT carries only PLACEHOLDER
    best_for/limits: those stay HUMAN-authored at the dock review."""
    fake = FakeProvider("fakesvc")
    DOCKING.dock("fakesvc", probe=True, loader=_loader_of(fake),
                 offers=[_offer("fakesvc", str(tmp_path))],
                 hermes_home=str(tmp_path))
    card_path = tmp_path / "plugins" / "memrack" / "backends.d" / "fakesvc.yaml"
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    assert card["draft"] is True
    assert card["name"] == card["provider"] == "fakesvc"
    module_field = card["module"]
    mod_path = pathlib.Path(module_field)
    if not mod_path.is_absolute():
        mod_path = card_path.parent / mod_path
    assert mod_path.exists(), "the draft card's module must resolve like every manifest"
    assert isinstance(card["search"], list), "derived (probe-outcome) search modes land in the card"
    assert card["best_for"].strip().upper().startswith("PLACEHOLDER"), card["best_for"]
    assert card["limits"].strip().upper().startswith("PLACEHOLDER"), card["limits"]
    assert card["evidence"]["probed_at"], card
    assert card["evidence"]["provider_version"] == fake.plugin_version
    assert card["mirror_tools"]["fake_1"] == "memrack__fakesvc__fake_1"
    # presence semantics: the card file in backends.d IS the registry input
    dock = MR.docked_dir(str(tmp_path))
    assert [p.name for p in dock.glob("*.yaml")] == ["fakesvc.yaml"]


def test_dock_offer_only_never_auto_docks(tmp_path):
    """§ Grundidee: 'erkennt … und bietet das Andocken an (`memrack dock
    <provider>`) — der Nutzer/Agent entscheidet' — NO `--probe` means NO card,
    only the offer; nothing docks silently."""
    fake = FakeProvider("fakesvc")
    out = DOCKING.dock("fakesvc", probe=False, loader=_loader_of(fake),
                       offers=[_offer("fakesvc", str(tmp_path))],
                       hermes_home=str(tmp_path))
    assert not (tmp_path / "plugins" / "memrack" / "backends.d" /
                "fakesvc.yaml").exists(), "an offer must never dock silently"
    assert out.get("offer"), "the answer IS the offer (user/agent decides)"


def test_dock_list_and_show_surface_effective_names(tmp_path):
    """Plan scope 3: `memrack dock list/show` subcommands — list shows the
    offers and the docked cards; show returns ONE card including the mirrored
    tools' effective names; § Namensgesetz: 'Effektive Namen stehen immer in
    der memrack_status-Card.'"""
    fake = FakeProvider("fakesvc")
    DOCKING.dock("fakesvc", probe=True, loader=_loader_of(fake),
                 offers=[_offer("fakesvc", str(tmp_path)),
                         _offer("otheroffer", str(tmp_path))],
                 hermes_home=str(tmp_path))
    listing = DOCKING.dock_list(offers=[_offer("otheroffer", str(tmp_path))],
                                hermes_home=str(tmp_path))
    names_cards = [c["provider"] for c in (listing.get("cards") or [])]
    assert "fakesvc" in names_cards, listing
    assert any(o["name"] == "otheroffer" for o in listing["offers"])
    shown = DOCKING.dock_show("fakesvc", hermes_home=str(tmp_path))
    assert shown["provider"] == "fakesvc"
    assert shown["draft"] is True
    assert shown["mirror_tools"]["fake_1"] == "memrack__fakesvc__fake_1"


def test_dock_remove_unregisters_by_presence(tmp_path):
    """Plan scope 3: `memrack dock remove` — the card file leaves backends.d
    (presence semantics: rm manifest = registered-removed); an absent card is
    an idempotent honest report, not an error."""
    fake = FakeProvider("fakesvc")
    DOCKING.dock("fakesvc", probe=True, loader=_loader_of(fake),
                 offers=[_offer("fakesvc", str(tmp_path))],
                 hermes_home=str(tmp_path))
    out = DOCKING.dock_remove("fakesvc", hermes_home=str(tmp_path))
    assert out["removed"] is True
    assert not (tmp_path / "plugins" / "memrack" / "backends.d" /
                "fakesvc.yaml").exists()
    again = DOCKING.dock_remove("fakesvc", hermes_home=str(tmp_path))
    assert again["removed"] is False and not again.get("error")


def test_drift_recheck_revisions_card_on_version_change(tmp_path):
    """Plan scope 4 + § Card-Erstellung: 'Conformance-Nachweis (Zeitstempel,
    Provider-Version; Drift-Recheck beim Update via --refresh)' — re-probing
    against a CHANGED provider version writes a card revision note with the
    version hop; the SAME version leaves the card untouched (no churn)."""
    fake = FakeProvider("fakesvc")
    offers = [_offer("fakesvc", str(tmp_path))]
    DOCKING.dock("fakesvc", probe=True, loader=_loader_of(fake),
                 offers=list(offers), hermes_home=str(tmp_path))
    card_path = tmp_path / "plugins" / "memrack" / "backends.d" / "fakesvc.yaml"
    first_version = yaml.safe_load(card_path.read_text(encoding="utf-8"))["evidence"]["provider_version"]

    fake.plugin_version = "10.0.0"        # the provider shipped an update
    DOCKING.dock("fakesvc", probe=False, refresh=True, loader=_loader_of(fake),
                 offers=list(offers), hermes_home=str(tmp_path))
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    assert card["revisions"], "a version change must leave a revision note"
    hop = card["revisions"][-1]
    assert hop["from_version"] == first_version
    assert hop["to_version"] == "10.0.0"

    same = FakeProvider("fakesvc")
    same.plugin_version = "10.0.0"
    DOCKING.dock("fakesvc", probe=False, refresh=True, loader=_loader_of(same),
                 offers=list(offers), hermes_home=str(tmp_path))
    card2 = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    assert len(card2["revisions"]) == 1, "same version -> no revision churn"


# ─────────────────────────────────────────────────────────────────────
# GREEN phase — fail-closed seams + core wiring (mirror dispatch, dock
# tool surface, status-card names, write-forward channel)
# ─────────────────────────────────────────────────────────────────────

class FakeColliding(FakeProvider):
    """Collision double: THE SAME tool registered twice — two mirror
    registrations fight over one effective name (the fail-closed case)."""

    def get_tool_schemas(self):
        return FakeProvider.get_tool_schemas(self)[:1] * 2


class _DuckCollider:
    """Minimal duck backend whose mirror set claims ONE name twice —
    conformance (the enforcement seam) must abort on it."""

    name = "collider"

    def initialize(self, session_id="", hermes_home=None,
                   provider_config=None, **kwargs):
        return None

    def on_turn(self, user, assistant, *, session_id=""):
        return None

    def on_memory_write(self, action, target, content, metadata=None, *,
                        session_id=""):
        return None

    def on_session_end(self, messages, *, session_id=""):
        return None

    def prefetch(self, query, *, session_id=""):
        return ""

    def system_prompt_block(self):
        return ""

    def shutdown(self):
        return None

    def export(self):
        return {"name": self.name}

    def tool_schemas(self):
        dup = {"function": {"name": "mr__collider__dup",
                            "description": "two of them",
                            "parameters": {"type": "object", "properties": {}}}}
        return [dict(dup), dict(dup)]


def test_probe_refuses_a_colliding_mirror_set_fail_closed(tmp_path):
    """§ Namensgesetz: 'Kollisionen -> Conformance bricht mit Fehler ab
    (fail-closed)' — the probe aborts with the error, nothing silently
    deduplicates, no card is written for it."""
    report = DOCKING.probe(
        "colliding", loader=_loader_of(FakeColliding("colliding")),
        hermes_home=str(tmp_path))
    assert report.get("error"), report
    assert "collision" in str(report["error"])
    assert report["mirror_tools"] == [], report
    out = DOCKING.dock("colliding", probe=True,
                       loader=_loader_of(FakeColliding("colliding")),
                       offers=[_offer("colliding", str(tmp_path))],
                       hermes_home=str(tmp_path))
    assert out.get("gap"), out
    assert not (tmp_path / "plugins" / "memrack" / "backends.d" /
                "colliding.yaml").exists(), "a refused dock never leaves a card"


def test_dock_refuses_to_clobber_a_reviewed_card(tmp_path):
    """§ Card-Erstellung / best_for/limits human-authored: once the card is
    REVIEWED (draft gone), a new probe draft never overwrites the human card
    text (fail-closed) — --refresh is the designed drift path."""
    fake = FakeProvider("fakesvc")
    DOCKING.dock("fakesvc", probe=True, loader=_loader_of(fake),
                 offers=[_offer("fakesvc", str(tmp_path))],
                 hermes_home=str(tmp_path))
    card_path = tmp_path / "plugins" / "memrack" / "backends.d" / "fakesvc.yaml"
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    del card["draft"]                     # the human review happened
    card["best_for"] = "humans wrote this after review"
    card_path.write_text(yaml.dump(card), encoding="utf-8")
    out = DOCKING.dock("fakesvc", probe=True, loader=_loader_of(fake),
                       offers=[_offer("fakesvc", str(tmp_path))],
                       hermes_home=str(tmp_path))
    assert out.get("gap") and "REVIEWED" in out["gap"].upper(), out
    assert "humans wrote this after review" in card_path.read_text(encoding="utf-8")


def test_conformance_fails_closed_on_mirror_name_collision():
    """Conformance = the enforcement seam (design: 'Kollisionen -> Conformance
    bricht mit Fehler ab'): a backend whose mirror set claims one name twice
    fails with a mirror-collision error — nothing is silently deduplicated."""
    report = CONF.check_backend(_DuckCollider())
    assert not report["ok"], report
    assert any(e.get("kind") == "mirror-collision" for e in report["errors"]), report


def test_proxy_passes_conformance(tmp_path):
    """The adapter AS a docked backend member passes the docking contract's
    conformance with a live double bound (required hooks + honest card; no
    provider import — the double carries the surface)."""
    proxy = PROXY.CatalogProxyBackend(provider=FakeProvider("fakesvc"),
                                      mirror_tools=True)
    proxy.initialize(session_id="CONF", hermes_home=str(tmp_path),
                     provider_config={})
    report = CONF.check_backend(proxy)
    assert report["ok"], report


def test_core_surfaces_dock_tool_and_mirrored_schemas(tmp_path):
    """The docking surface + the mirror law on the provider surface: the rack
    carries memrack_dock; a docked provider's tools appear under their
    MIRRORED names; the original names never reach the host."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake, mirror_tools=True)
    rack = MR._MemrackBase(config={})
    rack.initialize(session_id="S", hermes_home=str(tmp_path))
    rack._rack["fakesvc"] = proxy
    names = [s["function"]["name"] for s in rack.get_tool_schemas()]
    assert "memrack_dock" in names
    assert "memrack__fakesvc__fake_1" in names
    assert "fake_1" not in names and "fake_2" not in names and "fake_3" not in names


def test_core_dispatches_mirrored_calls_and_refuses_originals(tmp_path):
    """Umlenkung ③ through the REAL surface: the host calls the mirrored
    name and the provider answers its ORIGINAL tool; the ORIGINAL names are
    not host-callable (fail-closed — never a guessed tool)."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake, mirror_tools=True)
    rack = MR._MemrackBase(config={})
    rack.initialize(session_id="S", hermes_home=str(tmp_path))
    rack._rack["fakesvc"] = proxy
    out = rack.handle_tool_call("memrack__fakesvc__fake_3", {"k": 1})
    assert "fake_3" in str(out)
    assert "tool:fake_3" in fake.calls
    with pytest.raises(KeyError):
        rack.handle_tool_call("fake_3", {})


def test_core_dock_tool_answers(tmp_path):
    """The docking surface tool: an action without a provider = an honest
    error; the bare list answer carries BOTH inventories (offers + cards) —
    hermes-less it degrades to empty lists, never a crash."""
    rack = MR._MemrackBase(config={})
    rack.initialize(session_id="D", hermes_home=str(tmp_path))
    out = json.loads(rack.handle_tool_call("memrack_dock", {"action": "probe"}))
    assert "error" in out, out
    listing = json.loads(rack.handle_tool_call("memrack_dock", {}))
    assert isinstance(listing.get("offers"), list), listing
    assert isinstance(listing.get("cards"), list), listing


def test_status_card_carries_effective_names_and_draft(tmp_path):
    """§ Namensgesetz: 'Effektive Namen stehen immer in der memrack_status-
    Card' — the status cards carry the mirrored effective tool names and
    the draft marker until the human review ran."""
    fake = FakeProvider("fakesvc")
    proxy = PROXY.CatalogProxyBackend(provider=fake, mirror_tools=True)
    proxy.manifest = {"name": "fakesvc", "module": "fakesvc.py", "draft": True,
                      "search": [],
                      "mirror_tools": {"fake_1": "memrack__fakesvc__fake_1"}}
    rack = MR._MemrackBase(config={})
    rack.initialize(session_id="S", hermes_home=str(tmp_path))
    rack._rack["fakesvc"] = proxy
    status = json.loads(rack.handle_tool_call("memrack_status", {}))
    card = status["cards"]["fakesvc"]
    assert card["mirrored_tools"] == {"fake_1": "memrack__fakesvc__fake_1"}
    assert card["draft"] is True


def test_core_forwards_builtin_writes_to_recorded_docks(tmp_path):
    """Design row on_memory_write (Forward — je Provider entschieden) through
    the provider core: a dock whose card RECORDS forward_writes receives the
    builtin-memory write mirror; a dock without the record receives NOTHING
    (fail-closed). The routed receipt stays THE answer."""
    fake = FakeProvider("fakesvc")
    fwd = PROXY.CatalogProxyBackend(provider=fake, forward_writes=True)
    fwd.manifest = {"name": "fakesvc", "module": "fakesvc.py", "draft": True,
                    "forward_writes": True}
    quiet_fake = FakeProvider("quiet")
    quiet = PROXY.CatalogProxyBackend(provider=quiet_fake)
    quiet.manifest = {"name": "quiet", "module": "quiet.py", "draft": True}
    rack = MR._MemrackBase(config={})
    rack.initialize(session_id="W", hermes_home=str(tmp_path))
    rack._rack["fakesvc"] = fwd
    rack._rack["quiet"] = quiet
    receipt = rack.on_memory_write("add", "memory", "the fact")
    assert isinstance(receipt, dict), receipt
    assert fake.writes == [("add", "memory", "the fact", None)], fake.writes
    assert quiet_fake.writes == [], "no recorded decision -> receives nothing"
