"""Catalog-dock retrieval bridge + reference-dock proof tests.

Source of truth:
~/.hermes/projects/memrack/references/catalog-rack-adapter-design.md — the
proxy-design rows "Retrieval/Query-API: wenn Query-Methode vorhanden → Fan-out-
Namensgeber; wenn nur prefetch → Kontext-Engine-Kategorie" and Deviations #3
("the per-provider bridge is deliberate work proven against ONE real
provider — never a generic claim"), plus the drift-recheck card law
(human-authored card data survives a refresh).

The bridge is CARD-DRIVEN GENERIC code: the adapter reads a recorded
``query_bridge`` recipe from the card (backends.d/<provider>.yaml) and NEVER
guesses a provider's query signature. Provider specifics live only in card
data / probe evidence — the adapter stays provider-agnostic by law.

Reference-provider section: the REAL installed catalog provider is probed
(instantiation only — no initialize, no model, no network) whenever it is
installed on this machine; the section skips honestly when it is not
(the portable suite stays hermes-less and offline).
"""

import importlib.util
import json
import os
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


PROXY = _spec_load("memrack_catalog_proxy", "backends/_catalog_proxy.py")
DOCKING = _spec_load("memrack_docking", "memrack/docking.py")
MR = _spec_load("memrack", "__init__.py")
sys.modules["memrack"] = MR


class ToolDouble:
    """In-process double of a tool-centric catalog provider: one search-family
    tool (flat schema shape, like the reference provider ships)."""

    plugin_version = "9.9.9"

    def __init__(self, name="stubprov", contents=None, payload=None):
        self.name = name
        self.contents = list(contents or [])
        self.payload = payload
        self.calls = []
        self.initialize_kwargs = None

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.calls.append("initialize")
        self.initialize_kwargs = dict(kwargs)

    def shutdown(self):
        self.calls.append("shutdown")

    def get_tool_schemas(self):
        return [{
            "name": "recall",
            "description": "search-family tool (the provider answers tool calls)",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]},
        }]

    def handle_tool_call(self, tool_name, args, **kwargs):
        self.calls.append(("tool", tool_name, dict(args or {})))
        if self.payload is not None:
            return json.dumps(self.payload)
        return json.dumps({"results": [{"content": c} for c in self.contents]},
                          ensure_ascii=False)

    def system_prompt_block(self):
        return ""

    def prefetch(self, query, *, session_id=""):
        return ""

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        return None

    def on_memory_write(self, action, target, content, metadata=None):
        return None

    def on_session_end(self, messages):
        return None


def _bridge_manifest(name="stubprov", search=("vec",), bridge=None,
                     **extra):
    man = {"name": name, "module": "%s.py" % name, "search": list(search or [])}
    if bridge is None:
        bridge = {"via": "tool", "tool": "recall"}
    if bridge != "ABSENT":
        man["query_bridge"] = bridge
    man.update(extra)
    return man


def _bound_proxy(tmp_path, double=None, manifest=None, payload=None):
    double = double or ToolDouble(contents=["the recorded fact surfaced as a hit"],
                                  payload=payload)
    proxy = PROXY.CatalogProxyBackend(provider=double)
    proxy.initialize(session_id="S", hermes_home=str(tmp_path), provider_config={})
    proxy.manifest = manifest or _bridge_manifest(name=double.name)
    proxy.name = double.name
    return proxy, double


# ─────────────────────────────────────────────────────────────────────
# § the card-recorded query bridge (fan-out namesake — Deviations #3)
# ─────────────────────────────────────────────────────────────────────

def test_bridge_executes_the_recorded_recipe(tmp_path):
    """The adapter answers a claimed mode THROUGH the card's recorded recipe:
    the provider's search-family tool is called with its ORIGINAL name (the
    mirror law stays host-side) and the tool JSON arrives as rack hits."""
    proxy, double = _bound_proxy(tmp_path, double=ToolDouble(contents=[
        "recorded fact one", "recorded fact two"]))
    hits = proxy.search("needle", "vec", 5, session_id="S")
    assert hits[0]["backend"] == "stubprov"
    assert hits[0]["kind"] == "fact"
    assert hits[0]["rank"] == 1 and len(hits) == 2
    assert any("recorded fact two" in h["content"] for h in hits)
    # the provider saw its ORIGINAL tool call — never the mirrored name
    assert double.calls[-1][0] == "tool" and double.calls[-1][1] == "recall"


def test_bridge_capped_at_k_and_recipe_keys(tmp_path):
    """The recipe's data keys (results_key / query_arg / kind) drive the
    mapping; the engine hands at most k hits to the fusion."""

    class ShapedTool(ToolDouble):
        def get_tool_schemas(self):
            return [{"name": "lookup", "parameters": {}}]

        def handle_tool_call(self, tool_name, args, **kwargs):
            self.calls.append(("tool", tool_name, dict(args or {})))
            return json.dumps({"facts": [{"content": c, "id": i} for i, c in
                                         enumerate(self.contents)]})

    proxy, double = _bound_proxy(tmp_path, double=ShapedTool(contents=[
        "a hit", "b hit", "c hit", "d hit"]),
        manifest=_bridge_manifest(bridge={"via": "tool", "tool": "lookup",
                                          "query_arg": "needle",
                                          "results_key": "facts",
                                          "kind": "semantic"}))
    hits = proxy.search("q", "vec", 3)
    assert [h["rank"] for h in hits] == [1, 2, 3] and len(hits) == 3
    assert all(h["kind"] == "semantic" for h in hits)
    assert all(h["id"] is not None for h in hits)   # carried through when the tool has one
    assert double.calls[-1][2]["needle"] == "q"


def test_bridge_requires_the_recorded_recipe(tmp_path):
    """Fail-closed claim law: a card claiming modes WITHOUT a recorded recipe
    keeps raising — 'a defined method is NOT a claim, the CARD is' — no
    generic bridge is ever guessed."""
    proxy, _ = _bound_proxy(tmp_path)
    proxy.manifest = {"name": "stubprov", "module": "stubprov.py",
                      "search": ["vec"]}   # no query_bridge recorded
    with pytest.raises(ValueError):
        proxy.search("needle", "vec", 5)


def test_bridge_refuses_unclaimed_or_unavailable(tmp_path):
    """Direct engine calls stay honest: an UNCLAIMED mode and an UNAVAILABLE
    provider never answer with fake hits."""
    proxy, double = _bound_proxy(tmp_path)
    double.is_available = lambda: False
    proxy.manifest["search"] = ["vec"]
    with pytest.raises(ValueError):
        proxy.search("q", "fts", 5)           # not the card's mode
    out = proxy.card_modes()                  # unavailable → no modes promised
    assert out == ()


def test_bridge_provider_tool_error_fails_loud(tmp_path):
    """A provider tool answering an error payload is an ENGINE ERROR, never a
    silent zero-result (the honest-degrade law)."""
    proxy, double = _bound_proxy(tmp_path, payload={"error": "provider boom"})
    with pytest.raises(ValueError):
        proxy.search("q", "vec", 5)

    rack = _rack_with_proxy(tmp_path, proxy, "stubprov")
    out = json.loads(rack.handle_tool_call("memrack_search", {"query": "q"}))
    assert out["engine_errors"], "the error must surface, not vanish: %s" % out


def test_bridge_abstain_is_an_honest_zero(tmp_path):
    """The provider's own abstain (its no_confident_match shape) = no hits,
    NO engine error — zero results stay zero results, honestly."""
    proxy, _ = _bound_proxy(tmp_path,
                            payload={"results": [], "count": 0,
                                     "status": "no_confident_match"})
    assert proxy.search("q", "vec", 5) == []


def test_bridge_hits_enter_the_fanout_pool(tmp_path):
    """Design row 'Retrieval/Query-API → Fan-out namesake': the docked bridge
    runs as one rail of the WHOLE-rack fan-out — its hits sit in the merged
    pool next to the floor's hits, and the rail is named in found_by."""
    proxy, double = _bound_proxy(tmp_path, double=ToolDouble(contents=[
        "the bridge hit rides the pool"]))
    rack = _rack_with_proxy(tmp_path, proxy, "stubprov")
    out = json.loads(rack.handle_tool_call("memrack_search", {"query": "recorded"}))
    backends = {hit["backend"] for hit in out["results"]}
    assert "stubprov" in backends, out
    rail_hit = next(h for h in out["results"] if h["backend"] == "stubprov")
    assert "vec" in rail_hit["found_by"], rail_hit
    assert out["cards"], "the in-flow cards slot stays additive"


def _rack_with_proxy(tmp_path, proxy, name):
    rack = MR._MemrackBase(config={})
    rack.initialize(session_id="S", hermes_home=str(tmp_path))
    rack._rack[name] = proxy
    return rack


# ─────────────────────────────────────────────────────────────────────
# § pass-through binding home (the host's initialize contract)
# ─────────────────────────────────────────────────────────────────────

def test_proxy_forwards_the_binding_home(tmp_path):
    """The host binds providers with initialize(session_id, hermes_home=...):
    a docked provider through the proxy must SEE the binding home (its own
    store stays in ITS home) — never silently fall back to its own default."""
    double = ToolDouble(contents=[])
    proxy = PROXY.CatalogProxyBackend(provider=double)
    proxy.initialize(session_id="S1", hermes_home=str(tmp_path), provider_config={})
    assert double.initialize_kwargs.get("hermes_home") == str(tmp_path), \
        "the docked provider must be bound to the SAME home the rack was"


# ─────────────────────────────────────────────────────────────────────
# § drift recheck: human-authored retrieval claims survive the refresh
# ─────────────────────────────────────────────────────────────────────

def test_drift_recheck_keeps_authored_retrieval_claims(tmp_path):
    """Drift law: a version-hop refresh re-derives what the PROBE can derive;
    recorded card data (modes + bridge + human text) that the machine does
    not derive stays authored — the card never silently loses its claims."""
    home = tmp_path
    dock = home / "plugins" / "memrack" / "backends.d"
    dock.mkdir(parents=True)
    card = {"name": "stubprov", "provider": "stubprov", "module": "stubprov.py",
            "draft": True, "best_for": "authored best_for",
            "limits": "authored limits", "search": ["vec"],
            "query_bridge": {"via": "tool", "tool": "recall"},
            "evidence": {"probed_at": "2026-01-01T00:00:00.000000Z",
                         "provider_version": "1.0.0"},
            "revisions": []}
    (dock / "stubprov.yaml").write_text(yaml.dump(card), encoding="utf-8")

    fake = ToolDouble(name="stubprov", contents=[])
    fake.plugin_version = "2.0.0"           # the provider shipped an update
    offers = [{"name": "stubprov", "source": "user", "version": "2.0.0",
               "dir": str(home), "active": False}]
    def _load():
        return fake
    DOCKING.dock("stubprov", probe=False, refresh=True, loader=_load,
                 offers=list(offers), hermes_home=str(home))
    card_after = yaml.safe_load((dock / "stubprov.yaml").read_text(encoding="utf-8"))
    assert card_after["revisions"], "the version hop leaves its revision note"
    assert card_after["revisions"][-1]["to_version"] == "2.0.0"
    assert card_after["search"] == ["vec"], "authored mode claims survive"
    assert card_after["query_bridge"], "authored bridge survives"
    assert card_after["best_for"] == "authored best_for"
    assert card_after["limits"] == "authored limits"
    assert card_after["evidence"]["provider_version"] == "2.0.0"


# ─────────────────────────────────────────────────────────────────────
# § reference provider probe (the REAL installed plugin, instantiation
# only — no initialize, no models, no network; skips when not installed)
# ─────────────────────────────────────────────────────────────────────

def _hermes_agent_dir():
    return pathlib.Path.home() / ".hermes" / "hermes-agent"


def _limbic_plugin_dir():
    home = pathlib.Path.home() / ".hermes"
    active = pathlib.Path(__import__("os").environ.get("HERMES_HOME", "") or home)
    for cand in (active / "plugins" / "limbic", home / "plugins" / "limbic"):
        if (cand / "provider.py").is_file():
            return cand
    return None


def _limbic_hub_loader(hub):
    def _load():
        return hub.load_memory_provider("limbic", register_skills=False)
    return _load


_HERMES_HOME_BEFORE_PROBE = os.environ.get("HERMES_HOME", "")


def test_probe_of_the_real_installed_reference_provider():
    """The REAL provider class proves the ABC in isolation (the probing rules
    the dock by its OUTCOME, not by catalog category): occupancy, the honest
    per-provider classification, and the mirrored name set — with the SAME
    probe machinery `memrack dock <provider> --probe` drives."""
    plugin_dir = _limbic_plugin_dir()
    agent_dir = _hermes_agent_dir()
    if plugin_dir is None:
        pytest.skip("reference provider (limbic) not installed on this machine")
    if not (agent_dir / "agent" / "memory_provider.py").is_file():
        pytest.skip("hermes agent tree not available for the ABC import")
    # the hub scopes its discovery to HERMES_HOME: the probe must run in the
    # home the plugin is installed in (the CLI `dock --probe` does the same
    # against ITS home — this is the portable replica of that call).
    sys.path.insert(0, str(agent_dir))
    try:
        from plugins import memory as hub
    except Exception as exc:
        sys.path.remove(str(agent_dir))
        pytest.skip("memory-provider hub not importable hermes-less: %s" % exc)

    try:
        os.environ["HERMES_HOME"] = str(plugin_dir.parent.parent)
        report = DOCKING.probe("limbic", loader=_limbic_hub_loader(hub))
    finally:
        os.environ["HERMES_HOME"] = _HERMES_HOME_BEFORE_PROBE
        sys.path.remove(str(agent_dir))

    assert report.get("abc_occupied") is True, report
    assert report.get("provider_version"), report
    hooks = report.get("hooks") or {}
    for hook in ("is_available", "initialize", "get_tool_schemas", "prefetch",
                 "system_prompt_block", "on_memory_write", "on_session_end",
                 "shutdown"):
        assert hooks.get(hook) is True, (hook, report)
    assert report.get("classification") == "query-capable", report
    mirrored = {entry["tool"]: entry["effective"]
                for entry in report.get("mirror_tools") or []}
    assert set(mirrored) == {"remember", "recall", "forget"}, mirrored
    for original, effective in mirrored.items():
        assert not original.startswith(("memrack__", "mr__"))
        assert effective.startswith(("memrack__", "mr__"))
        assert len(effective) <= 64
        assert effective == mirrored[original], "restart-stable alias"
    assert report.get("available") is True, report
