"""Round-4 behavior bundle — seven scopes pinned BEFORE the fixes exist (RED):

1.  Per-card transcript gate: a docked member whose card records
    ``forward_transcript: true`` receives the ``on_session_end`` transcript;
    without that recorded decision a dock receives the session-end EVENT
    with no transcript (fail-closed, human-authored like forward_writes /
    forward_turns), while code-first built-ins keep today's behavior
    byte-identical. The gate rides the status card; the draft card names it.
2.  The hook fan-out runs in parallel with a per-backend timeout
    (``fanout_timeout_s``, default 5 s): a hanging engine becomes an honest
    engine_errors gap while the rest still delivers, in the deterministic
    rack order; the merged prefetch block carries a total character budget
    (``prefetch_max_chars``, default 4000) that truncates honestly with a
    "+N chars truncated" note. Threads ride the host's
    ``spawn_context_thread`` when importable, a plain daemon thread
    otherwise; both schema surfaces declare the two limits.
3.  The memory-MIRROR path (host memory tool writes) is suppressed when the
    provider runs as cron/subagent (initialize-recorded or per-call), with
    an honest route-log note; explicit ``memrack_write`` tool calls stay
    agent decisions, and absent/unknown contexts behave exactly as today.
4.  A DEPLOYED config-schema copy derives its home from ``Path(__file__)``
    (valid only while the sibling ``plugins/memrack`` directory exists) so a
    shared process never resolves the wrong profile home; repo checkouts
    fall back to ``$HERMES_HOME`` exactly as before.
5.  Draft cards carry the honest checkpoint line: the rack sets no
    ``pre_compress_checkpoint_api_version``, so a docked provider's
    fail-closed checkpoint guarantee cannot be claimed through memrack.
6.  Cross-process exclusivity for the initialize side-effect block: an
    flock on ``plugin-data/memrack/init.lock`` serializes the fold, the
    uninstall grace and the dock_want consumption; a busy lock degrades
    with an honest note, read paths stay unlocked, and a concurrent
    initialize can never consume the same dock_want twice.
7.  Config writes prefer the host's own helpers when importable
    (``hermes_cli.config.save_config`` for config.yaml blocks,
    ``utils.atomic_json_write`` for the flat json sidecar) with the guarded
    in-plugin writers as the hermes-less fallback — merge per key, atomic,
    0600 stand in both modes.
"""
import importlib.util
import json
import pathlib
import sys
import threading
import time

import pytest  # noqa: E402
import yaml  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr  # noqa: E402


def _spec_load(mod_name, relpath):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _provider(tmp_path, config=None, **init_kwargs):
    prov = mr._MemrackBase(config=dict(config or {}))
    prov.initialize(session_id="R4", hermes_home=str(tmp_path), **init_kwargs)
    return prov


_HOME_SEQ = iter(range(100000))


def _fresh_home(tmp_path) -> pathlib.Path:
    home = tmp_path / ("r4-home-%d" % next(_HOME_SEQ))
    home.mkdir(parents=True, exist_ok=True)
    return home


# ═════════════════════════════════════════════════════════════════════════
# 1) the per-card transcript gate (on_session_end fan-out)
# ═════════════════════════════════════════════════════════════════════════

_DOCK_STUB = """
class Be:
    name = %(name)r

    def __init__(self):
        self.seen = []

    def initialize(self, session_id="", hermes_home=None, **kw):
        pass

    def on_turn(self, user, assistant, *, session_id=""):
        pass

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        pass

    def on_session_end(self, messages, *, session_id=""):
        self.seen.append((messages, session_id))
        return {"who": %(name)r}

    def prefetch(self, query, *, session_id=""):
        return ""

    def system_prompt_block(self):
        return ""

    def shutdown(self):
        pass

    def export(self):
        return {"name": self.name}


def export_backend():
    return Be()
"""


def _dock_dir(tmp_path):
    return tmp_path / "plugins" / "memrack" / "backends.d"


def _facts(home):
    """The base-db rows off the store file (id, content)."""
    db = pathlib.Path(home) / "plugin-data" / "memrack" / "memrack.db"
    if not db.exists():
        return []
    import sqlite3
    con = sqlite3.connect(str(db))
    try:
        return [{"id": row[0], "content": row[1]} for row in
                con.execute("SELECT id, content FROM facts ORDER BY id")]
    finally:
        con.close()


def _manifest_dock(home, name, card):
    dock = _dock_dir(home)
    dock.mkdir(parents=True, exist_ok=True)
    (dock / (name + ".py")).write_text(_DOCK_STUB % {"name": name},
                                       encoding="utf-8")
    card = dict(card, name=name, module=name + ".py")
    (dock / (name + ".yaml")).write_text(
        yaml.dump(card, sort_keys=False), encoding="utf-8")


def test_gated_dock_receives_the_transcript(tmp_path):
    home = _fresh_home(tmp_path)
    _manifest_dock(home, "gated", {"forward_transcript": True})
    prov = _provider(home, {"enabled_backends": "base-db"})
    transcript = [{"role": "user", "content": "the session transcript"}]
    prov.on_session_end(transcript)
    docked = prov._rack["gated"]
    assert docked.seen, "the gated dock must receive the session end"
    messages, session_id = docked.seen[0]
    assert messages is transcript, "the transcript rides byte-identical"
    assert session_id == "R4", "the session scoping is the provider's own"


def test_ungated_dock_receives_the_event_without_the_transcript(tmp_path):
    home = _fresh_home(tmp_path)
    _manifest_dock(home, "plain", {})              # no recorded decision
    _manifest_dock(home, "explicit_off", {"forward_transcript": False})
    prov = _provider(home, {"enabled_backends": "base-db"})
    transcript = [{"role": "user", "content": "nobody may copy this"}]
    prov.on_session_end(transcript)
    for name in ("plain", "explicit_off"):
        docked = prov._rack[name]
        assert docked.seen, "%s still gets the session-end event" % name
        messages, _ = docked.seen[0]
        assert messages is None, "%s must not carry the transcript" % name


def test_legacy_dock_without_card_is_fail_closed(tmp_path):
    """A code-first drop-in dock (no card carrying a recorded decision)
    receives the event, never the transcript."""
    dock = _dock_dir(_fresh_home(tmp_path))
    dock.mkdir(parents=True, exist_ok=True)
    (dock / "legacy.py").write_text(_DOCK_STUB % {"name": "legacy"},
                                    encoding="utf-8")
    prov = _provider(dock.parent.parent.parent, {"enabled_backends": "base-db"})
    transcript = [{"role": "user", "content": "legacy sees no copy"}]
    prov.on_session_end(transcript)
    legacy = prov._rack["legacy"]
    assert legacy.seen and legacy.seen[0][0] is None, legacy.seen


def test_builtins_keep_the_transcript_byte_identical(tmp_path):
    home = _fresh_home(tmp_path)
    prov = _provider(home, {"enabled_backends": "base-db,state-episodic"})
    transcript = [{"role": "user", "content": "built-ins stay exactly as today"}]
    captured = []
    engine = prov._rack["base-db"]
    engine.on_session_end = lambda messages, **kw: captured.append(messages)
    prov.on_session_end(transcript, session_id="R4B")
    assert captured == [transcript], captured
    assert captured[0] is transcript, "built-ins ride the same object (byte-identical)"
    # the payload gate helper itself is an identity pass for built-ins
    args, kwargs = (transcript,), {"session_id": "S"}
    assert mr._session_end_payload(engine, args, kwargs) == (args, kwargs)
    out = args  # the gate helper must not copy or reshape
    assert mr._session_end_payload(engine, args, kwargs)[0] is out


def test_gate_rides_the_status_card(tmp_path):
    home = _fresh_home(tmp_path)
    _manifest_dock(home, "gated", {"forward_transcript": True})
    _manifest_dock(home, "plain", {})
    prov = _provider(home, {"enabled_backends": "base-db"})
    cards = prov._cards_now()
    assert cards["gated"].get("forward_transcript") is True, cards["gated"]
    assert "forward_transcript" not in cards["plain"], cards["plain"]


def test_gate_failures_stay_isolated(tmp_path, caplog):
    home = _fresh_home(tmp_path)
    _manifest_dock(home, "gated", {"forward_transcript": True})
    _manifest_dock(home, "plain", {})
    _manifest_dock(home, "plain-b", {})
    prov = _provider(home, {"enabled_backends": "base-db"})
    gated = prov._rack["gated"]
    def _boom(messages, *, session_id=""):
        raise RuntimeError("gated dock blew up")
    gated.on_session_end = _boom
    transcript = [{"role": "user", "content": "x"}]
    out = prov.on_session_end(transcript)
    assert out["plain"] == {"who": "plain"}, out
    assert out["plain-b"] == {"who": "plain-b"}, out
    assert "gated" in [e["backend"] for e in out["engine_errors"]], out
    assert "gated dock blew up" in caplog.text


# ═════════════════════════════════════════════════════════════════════════
# 2) parallel fan-out + per-backend timeout + prefetch budget
# ═════════════════════════════════════════════════════════════════════════

_SLOW_STUB = """
import time
import json

class Be:
    name = %(name)r
    sleep = %(sleep)r

    def __init__(self):
        self.seen = []

    def initialize(self, session_id="", hermes_home=None, **kw):
        pass

    def on_turn(self, user, assistant, *, session_id=""):
        pass

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        pass

    def on_session_end(self, messages, *, session_id=""):
        time.sleep(self.sleep)
        self.seen.append((messages, session_id))
        return {"who": %(name)r}

    def prefetch(self, query, *, session_id=""):
        return "PREF-" + %(name)r + " " + ("x" * %(prefetch_chars)r)

    def system_prompt_block(self):
        return "SPB " + %(name)r

    def shutdown(self):
        pass

    def export(self):
        return {"name": self.name}


def export_backend():
    return Be()
"""


def _slow_dock(home, name, seconds, prefetch_chars=0):
    dock = _dock_dir(home)
    dock.mkdir(parents=True, exist_ok=True)
    (dock / (name + ".py")).write_text(_SLOW_STUB % {
        "name": name, "sleep": seconds, "prefetch_chars": prefetch_chars},
        encoding="utf-8")


def test_fanout_is_parallel(tmp_path):
    home = _fresh_home(tmp_path)
    for name in ("slow-a", "slow-b", "slow-c"):
        _slow_dock(home, name, 0.5)
    prov = _provider(home, {"enabled_backends": "base-db",
                            "fanout_timeout_s": 5})
    started = time.monotonic()
    out = prov.on_session_end(None, session_id="R4P")
    elapsed = time.monotonic() - started
    assert out["slow-a"] == {"who": "slow-a"}, out
    assert out["slow-b"] == {"who": "slow-b"}, out
    assert out["slow-c"] == {"who": "slow-c"}, out
    assert elapsed < 1.2, ("three 0.5 s engines must overlap (~0.5 s), not "
                           "run one after another (~1.5 s)", elapsed)


def test_hanging_engine_becomes_an_honest_gap(tmp_path, caplog):
    home = _fresh_home(tmp_path)
    _slow_dock(home, "fast-a", 0)
    _slow_dock(home, "fast-b", 0)
    _slow_dock(home, "hanging", 3.0)
    prov = _provider(home, {"enabled_backends": "base-db",
                            "fanout_timeout_s": 0.2})
    started = time.monotonic()
    out = prov.on_session_end(None, session_id="R4T")
    elapsed = time.monotonic() - started
    assert elapsed < 1.5, ("the call never waits for the 3 s engine", elapsed)
    assert out["fast-a"] == {"who": "fast-a"}, out
    assert out["fast-b"] == {"who": "fast-b"}, out
    gaps = out.get("engine_errors") or []
    assert [g["backend"] for g in gaps] == ["hanging"], gaps
    assert "timed out" in gaps[0]["error"], gaps
    assert "fanout_timeout_s" in gaps[0]["error"], gaps
    assert prov._fanout_timeout_s() == 0.2


def test_slow_backend_gap_rest_runs(tmp_path, caplog):
    """The plan's pin: a sleep beyond the budget → gap entry, others deliver
    and the call returns inside the budget (never hangs on one engine)."""
    home = _fresh_home(tmp_path)
    _slow_dock(home, "quick", 0)
    _slow_dock(home, "quick2", 0)
    _slow_dock(home, "stuck", 2.5)
    prov = _provider(home, {"enabled_backends": "base-db",
                            "fanout_timeout_s": 0.15})
    started = time.monotonic()
    out = prov.on_session_end(None, session_id="R4G")
    elapsed = time.monotonic() - started
    assert elapsed < 1.5, ("the fan-out must not wait for the 2.5 s engine",
                           elapsed)
    assert out["quick"] == {"who": "quick"}, out
    assert out["quick2"] == {"who": "quick2"}, out
    gaps = out.get("engine_errors") or []
    assert [g["backend"] for g in gaps] == ["stuck"], gaps
    assert "fanout_timeout_s" in gaps[0]["error"] or "timed out" in gaps[0]["error"], gaps
    assert "stuck" in caplog.text, "the gap is logged honestly"


def test_fanout_results_collect_in_rack_order(tmp_path):
    home = _fresh_home(tmp_path)
    _slow_dock(home, "alpha", 0.12)     # slower engine finishing LAST
    _slow_dock(home, "beta", 0.02)
    _slow_dock(home, "gamma", 0.05)
    prov = _provider(home, {"enabled_backends": "base-db",
                            "fanout_timeout_s": 5})
    out = prov.on_session_end(None, session_id="R4O")
    assert list(out.keys()) == ["alpha", "beta", "gamma"], \
        "the collection is rack-ordered, not completion-ordered"


def test_fanout_timeout_reader_falls_back_to_default(tmp_path):
    prov = mr._MemrackBase(config={})
    assert prov._fanout_timeout_s() == 5.0, "the declared default"
    for value in ("nope", "", -1, 0, None):
        prov2 = mr._MemrackBase(config={"fanout_timeout_s": value})
        assert prov2._fanout_timeout_s() == 5.0, value


def test_prefetch_budget_truncates_with_a_honest_note(tmp_path):
    home = _fresh_home(tmp_path)
    _slow_dock(home, "alpha", 0, prefetch_chars=100)
    _slow_dock(home, "beta", 0, prefetch_chars=100)
    prov = _provider(home, {"enabled_backends": "base-db",
                            "prefetch_max_chars": 30})
    merged = prov.prefetch("q")
    assert merged.startswith("PREF-alpha"), "the first rack part stays"
    assert "[+" in merged and "chars truncated" in merged, merged
    body = merged.split("\n\n[")[0]
    assert len(body) == 30, (len(body), merged)


def test_prefetch_budget_under_limit_is_byte_identical(tmp_path):
    home = _fresh_home(tmp_path)
    _slow_dock(home, "alpha", 0, prefetch_chars=6)
    _slow_dock(home, "beta", 0, prefetch_chars=6)
    prov = _provider(home, {"enabled_backends": "base-db",
                            "prefetch_max_chars": 4000})
    assert prov.prefetch("q") == "PREF-alpha xxxxxx\n\nPREF-beta xxxxxx"


def test_prefetch_budget_reader_falls_back_to_default():
    prov = mr._MemrackBase(config={})
    assert prov._prefetch_max_chars() == 4000, "the declared default"
    for value in ("nope", "", -5, 0, None):
        prov2 = mr._MemrackBase(config={"prefetch_max_chars": value})
        assert prov2._prefetch_max_chars() == 4000, value


def test_schema_declares_the_two_limits():
    keys = {f["key"]: f for f in mr._MemrackBase().get_config_schema()}
    fanout = keys.get("fanout_timeout_s")
    budget = keys.get("prefetch_max_chars")
    assert fanout and budget, list(keys)
    assert fanout["description"], fanout
    assert fanout["default"] == 5, fanout
    assert budget["default"] == 4000, budget
    assert budget["description"], budget


def test_declared_schema_declares_the_two_limits(tmp_path, monkeypatch):
    pytest.importorskip(
        "plugins.memory.config_schema",
        reason="declared-surface tests need the Hermes runtime")
    home = _fresh_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec2 = importlib.util.spec_from_file_location(
        "_r4_declared", _ROOT / "config_schema.py")
    mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(mod)
    fields = {f.key: f for f in mod.CONFIG_SCHEMA.fields}
    fanout, budget = fields.get("fanout_timeout_s"), fields.get("prefetch_max_chars")
    assert fanout and budget, list(fields)
    assert fanout.default in (5, "5"), fanout
    assert budget.default in (4000, "4000"), budget
    static = [f.key for f in mod.CONFIG_SCHEMA.fields]
    assert static.index("fanout_timeout_s") > static.index("ollama_host"), \
        "the limits ride after the byte-identical base fields"


# ═════════════════════════════════════════════════════════════════════════
# 3) the memory-mirror path respects cron/subagent contexts
# ═════════════════════════════════════════════════════════════════════════

def _route_log(home):
    log = pathlib.Path(home) / "plugin-data" / "memrack" / "route_log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in
            log.read_text(encoding="utf-8").splitlines()]


def test_cron_mirror_write_is_skipped_with_a_note(tmp_path):
    home = _fresh_home(tmp_path)
    prov = _provider(home, {"enabled_backends": "base-db"}, agent_context="cron")
    assert getattr(prov, "_agent_context", None) == "cron"
    out = prov.on_memory_write("add", "memory", "an automatic cron fact")
    assert isinstance(out, dict) and out.get("status") == "skipped", out
    rows = _facts(home)
    assert rows == [], "no cron write reached the store"
    notes = [e for e in _route_log(home) if "skip" in str(e.get("kind", ""))]
    assert notes, _route_log(home)
    assert "cron" in json.dumps(notes[0]), notes


def test_subagent_write_skips_via_the_per_call_channel(tmp_path):
    home = _fresh_home(tmp_path)
    prov = _provider(home, {"enabled_backends": "base-db"})   # context recorded at init
    out = prov.on_memory_write("add", "memory", "subagent fact",
                               agent_context="subagent")
    assert isinstance(out, dict) and out.get("status") == "skipped", out
    assert _facts(home) == [], _facts(home)


def test_primary_and_absent_context_process_writes(tmp_path):
    home = _fresh_home(tmp_path)
    for ctx in (None, "primary", "weird-unknown"):
        prov = _provider(home, {"enabled_backends": "base-db"},
                         agent_context=ctx)
        out = prov.on_memory_write("add", "memory", "fact under %r" % ctx)
        assert not isinstance(out, dict) or out.get("status") != "skipped", out
        assert any("fact under" in (r.get("content") or "")
                   for r in _facts(home)), _facts(home)


def test_tool_write_stays_a_decision_in_cron(tmp_path):
    home = _fresh_home(tmp_path)
    prov = _provider(home, {"enabled_backends": "base-db"}, agent_context="cron")
    answer = json.loads(prov.handle_tool_call(
        "memrack_write", {"content": "the agent chose this", "backend": "base-db"}))
    assert answer.get("status") == "stored", answer
    assert any("the agent chose this" in (r.get("content") or "")
               for r in _facts(home)), _facts(home)
    # forward_writes-style mirror decisions of DOCKS skip the same way
    out = prov.on_memory_write("add", "memory", "another cron write")
    assert out.get("status") == "skipped", out


# ═════════════════════════════════════════════════════════════════════════
# 4) deployed copies derive their home from __file__
# ═════════════════════════════════════════════════════════════════════════

def _copy_plugin_module(source: pathlib.Path, target: pathlib.Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _simulate_profile_tree(tmp_path, home_name, env_hermes_home):
    """One deployed copy inside ANOTHER home than the process ENV names."""
    home = tmp_path / home_name
    plugin_root = home / "plugins" / "memrack"
    dock = plugin_root / "backends.d"
    (dock / "memrack" / "__copy__").mkdir(parents=True, exist_ok=True)
    card = {"name": "dock-%s" % home_name, "module": "m-%s.py" % home_name}
    (dock / ("dock-%s.yaml" % home_name)).write_text(
        yaml.dump(card, sort_keys=False), encoding="utf-8")
    _copy_plugin_module(_ROOT / "config_schema.py",
                        plugin_root / "config_schema.py")
    for rel in ("config_surface.py", "docking.py"):
        _copy_plugin_module(_ROOT / "memrack" / rel,
                            plugin_root / "memrack" / rel)
    monkey_target = {"env": str(env_hermes_home)}
    return home, monkey_target


def test_deployed_copy_derives_its_own_home(tmp_path, monkeypatch):
    pytest.importorskip(
        "plugins.memory.config_schema",
        reason="the declared schema module needs the Hermes runtime")
    other_home = tmp_path / "env-home"
    other_home.mkdir(parents=True, exist_ok=True)
    home_a, _ = _simulate_profile_tree(
        tmp_path, "profile-a", other_home)
    home_b, _ = _simulate_profile_tree(
        tmp_path, "profile-b", other_home)
    dock_names = []
    for label, home in (("a", home_a), ("b", home_b)):
        monkeypatch.setenv("HERMES_HOME", str(other_home))
        plugin_root = home / "plugins" / "memrack"
        spec2 = importlib.util.spec_from_file_location(
            "_r4_deployed_%s" % label, plugin_root / "config_schema.py")
        mod = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(mod)
        names = [f.key for f in mod.CONFIG_SCHEMA.fields
                 if f.key.endswith("_in_rack")]
        wanted = ("dock-%s_in_rack" % ("profile-%s" % label))
        assert wanted in names, (label, names, mod._hermes_home())
        assert wanted == "dock-profile-%s_in_rack" % label, wanted
        dock_names.append(wanted)
    assert "dock-profile-a_in_rack" in dock_names
    assert "dock-profile-b_in_rack" in dock_names
    # the seams carry the same derivation
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    spec3 = importlib.util.spec_from_file_location(
        "_r4_deployed_cs", home_a / "plugins" / "memrack" / "memrack" /
        "config_surface.py")
    csmod = importlib.util.module_from_spec(spec3)
    spec3.loader.exec_module(csmod)
    assert csmod._docked_dir() == home_a / "plugins" / "memrack" / "backends.d", \
        csmod._docked_dir()
    assert csmod._store_home() == home_a
    # an explicit home argument still wins (today's contract)
    assert csmod._docked_dir(str(other_home)) == \
        other_home / "plugins" / "memrack" / "backends.d"


def test_repo_context_falls_back_to_env(tmp_path, monkeypatch):
    pytest.importorskip(
        "plugins.memory.config_schema",
        reason="the declared schema module needs the Hermes runtime")
    env_home = tmp_path / "env-home"
    env_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(env_home))
    spec2 = importlib.util.spec_from_file_location(
        "_r4_repo_schema", _ROOT / "config_schema.py")
    mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(mod)
    assert mod._hermes_home() == env_home, "repo layout keeps the ENV law"
    cs = sys.modules.get("memrack_config_surface")
    assert cs is not None
    assert cs._docked_dir() == env_home / "plugins" / "memrack" / "backends.d"


# ═════════════════════════════════════════════════════════════════════════
# 5) the honest checkpoint line on draft cards
# ═════════════════════════════════════════════════════════════════════════

CHECKPOINT_LINE = ("checkpoint: not forwarded (pre_compress_checkpoint_api_version "
                   "is not set by the rack)")
DOCKING = None


def test_draft_card_names_the_checkpoint_contract():
    docking = _spec_load("memrack_docking_r4", "memrack/docking.py")
    report = {
        "provider": "fakesvc", "abc_occupied": True,
        "classification": "prefetch-only", "hooks": {},
        "mirror_tools": [], "derived_search_modes": [],
        "provider_version": "9.9.9",
        "evidence": {"probed_at": "2026-01-01T00:00:00.000000Z"},
    }
    card = docking.draft_card("fakesvc", report)
    assert str(card.get("checkpoint") or "") == \
        "not forwarded (pre_compress_checkpoint_api_version is not set by the rack)", card
    assert card.get("dock_boundary"), card


def test_draft_card_boundary_names_the_transcript_gate():
    docking = _spec_load("memrack_docking_r4b", "memrack/docking.py")
    report = {"provider": "fakesvc", "abc_occupied": True,
              "hooks": {}, "mirror_tools": [], "derived_search_modes": [],
              "evidence": {}}
    card = docking.draft_card("fakesvc", report)
    line = str(card.get("dock_boundary") or "")
    assert "forward_transcript" in line, line
    assert "transcript" in line, line


# ═════════════════════════════════════════════════════════════════════════
# 6) the init side-effect lock
# ═════════════════════════════════════════════════════════════════════════

class _ProbeSpyDocking:
    """Machine double counting every probe it receives."""

    def __init__(self):
        self.probes = []

    def dock(self, provider_name, **kwargs):
        self.probes.append((provider_name, dict(kwargs)))
        return {"provider": provider_name, "status": "draft-docked",
                "spy": True}


def _seed_dock_want(home, choice="fakeprov"):
    home = pathlib.Path(home)
    (home / "memrack").mkdir(parents=True, exist_ok=True)
    (home / "memrack" / "config.json").write_text(
        json.dumps({"dock_want": choice}), encoding="utf-8")
    (home / "config.yaml").write_text(
        yaml.dump({"plugins": {"memrack": {"dock_want": choice}}},
                  sort_keys=False), encoding="utf-8")
    return {"dock_want": choice}


def _spy_as_machine(spy, key="memrack_docking_core", prev=None):
    real = prev if prev is not None else sys.modules.get(key)
    sys.modules[key] = spy
    return real


def test_parallel_initializes_consume_dock_want_once(tmp_path):
    """Two concurrent initializes race the SAME home: the flock serializes
    the side-effect block, the second runner sees the one-shot receipt in
    the fresh store stand and never re-probes."""
    spy = _ProbeSpyDocking()
    home = _fresh_home(tmp_path)
    _seed_dock_want(home, "fakeprov")
    prev = _spy_as_machine(spy)
    cs = mr._config_surface_module()
    original_hook = cs.dock_want_hook
    barrier = threading.Barrier(2, timeout=3)

    def tracked(config, hermes_home=None, **kwargs):
        try:
            barrier.wait()
        except Exception:
            pass       # a broken barrier = the other runner never arrived
        return original_hook(config, hermes_home=hermes_home, **kwargs)

    try:
        cs.dock_want_hook = tracked
        outcomes = {}

        def runner(prov_name):
            prov = mr._MemrackBase()
            outcomes[prov_name] = prov.initialize(
                session_id="R4LOCK", hermes_home=str(home))

        threads = [threading.Thread(target=runner, args=(n,),
                                    name="runner-%s" % n)
                   for n in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    finally:
        cs.dock_want_hook = original_hook
        _spy_as_machine(prev, prev=prev)
    assert len(spy.probes) == 1, spy.probes
    flat = json.loads((home / "memrack" / "config.json").read_text(
        encoding="utf-8-sig"))
    assert flat.get("dock_want") is None, flat
    assert (flat.get("dock_want_last") or {}).get("provider") == "fakeprov", flat
    notes = _route_log(home)
    assert any("already consumed" in str(e.get("detail", "")) for e in notes), \
        "the concurrent double-consume is named honestly: %s" % (notes,)


def test_init_side_effect_lock_file_lands(tmp_path):
    home = _fresh_home(tmp_path)
    _provider(home, {"enabled_backends": "base-db"})
    lock = home / "plugin-data" / "memrack" / "init.lock"
    assert lock.exists(), lock


def test_busy_lock_skips_side_effects_honestly(tmp_path, monkeypatch, caplog):
    home = _fresh_home(tmp_path)
    _seed_dock_want(home, "fakeprov")
    spy = _ProbeSpyDocking()
    prev = _spy_as_machine(spy)
    import fcntl as real_fcntl
    original, original_lock = real_fcntl.flock, None

    def _busy(fd, mode):
        raise BlockingIOError("resource busy (test stub)")

    try:
        monkeypatch.setattr(real_fcntl, "flock", _busy)
        prov = mr._MemrackBase()
        prov.initialize(session_id="R4BUSY", hermes_home=str(home))
    finally:
        real_fcntl.flock = original
        _spy_as_machine(prev, prev=prev)
    assert spy.probes == [], "a busy lock consumes nothing"
    flat = json.loads((home / "memrack" / "config.json").read_text(
        encoding="utf-8-sig"))
    assert flat.get("dock_want") == "fakeprov", \
        "the choice stays for the honest retry: %s" % flat
    # the rack still works (the Basisfall duty)
    status = json.loads(prov.handle_tool_call("memrack_status", {}))
    assert status["backends"] == ["base-db"], status
    notes = _route_log(home)
    assert any("side effects skipped" in str(e.get("detail", "")) or
               "lock" in str(e.get("detail", "")) for e in notes), notes
    assert "lock" in caplog.text.lower()


# ═════════════════════════════════════════════════════════════════════════
# 7) config writes prefer the host helpers (guarded)
# ═════════════════════════════════════════════════════════════════════════

def test_save_config_prefers_the_host_yaml_writer(monkeypatch, tmp_path):
    home = _fresh_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "agent:\n  name: house\nplugins:\n  memrack:\n    old_key: 1\n",
        encoding="utf-8")
    calls = {}

    def fake_save_config(config, *, merge_existing=False, preserve_keys=None,
                         **kwargs):
        calls["config"] = config
        calls["merge_existing"] = merge_existing
        yaml_path = home / "config.yaml"

        def merge(base, override):
            out = dict(base)
            for key, value in override.items():
                out[key] = merge(value, base[key]) if \
                    isinstance(value, dict) and isinstance(base.get(key), dict) \
                    else value
            return out
        merged = merge(yaml.safe_load(yaml_path.read_text("utf-8-sig")) or {},
                       config)
        yaml_path.write_text(yaml.dump(merged, sort_keys=False),
                             encoding="utf-8")

    monkeypatch.setattr(mr, "_host_yaml_writer", lambda home_: fake_save_config)
    monkeypatch.setattr(mr, "_host_json_writer", lambda: None)
    prov = mr._MemrackBase()
    prov.save_config({"enabled_backends": "base-db,wiki"})
    assert calls.get("merge_existing") is True, calls
    block = calls["config"]["plugins"]["memrack"]
    assert block == {"old_key": 1, "enabled_backends": "base-db,wiki"}, block
    # the host-writer path's own file: the fake wrote the merge per key
    data = yaml.safe_load((home / "config.yaml").read_text("utf-8-sig")) or {}
    assert data["agent"] == {"name": "house"}, data
    assert data["plugins"]["memrack"]["old_key"] == 1, data
    assert data["plugins"]["memrack"]["enabled_backends"] == "base-db,wiki", data


def test_save_config_fallback_mode_keeps_the_in_plugin_writer(monkeypatch, tmp_path):
    home = _fresh_home(tmp_path)
    (home / "config.yaml").write_text(
        "plugins:\n  memrack:\n    old_key: 1\n", encoding="utf-8")
    monkeypatch.setattr(mr, "_host_yaml_writer", lambda home_: None)
    monkeypatch.setattr(mr, "_host_json_writer", lambda: None)
    prov = mr._MemrackBase()
    prov.save_config({"search_top_k": 12}, str(home))
    data = yaml.safe_load((home / "config.yaml").read_text("utf-8-sig")) or {}
    block = data["plugins"]["memrack"]
    assert block == {"old_key": 1, "search_top_k": 12}, block
    flat = json.loads((home / "memrack" / "config.json").read_text(
        encoding="utf-8-sig"))
    assert flat == {"search_top_k": 12}, flat
    assert (home / "config.yaml").stat().st_mode & 0o777 == 0o600


def test_save_config_json_sidecar_uses_the_host_writer(monkeypatch, tmp_path):
    home = _fresh_home(tmp_path)
    (home / "memrack").mkdir(parents=True, exist_ok=True)
    (home / "memrack" / "config.json").write_text(
        json.dumps({"search_top_k": 4, "kept": "x"}), encoding="utf-8")
    seen = {}

    def fake_json_write(path, data, *args, **kwargs):
        seen["path"] = pathlib.Path(path)
        seen["data"] = data
        seen["kwargs"] = kwargs
        seen["path"].write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(mr, "_host_json_writer", lambda: fake_json_write)
    prov = mr._MemrackBase()
    prov.save_config({"search_top_k": 9}, str(home))
    assert seen["path"] == home / "memrack" / "config.json", seen
    assert seen["data"] == {"search_top_k": 9, "kept": "x"}, "merge per key"
    assert seen["kwargs"].get("mode") == 0o600, seen["kwargs"]


def test_flat_seam_writer_prefers_the_host_json_writer(monkeypatch, tmp_path):
    cs = _spec_load("memrack_config_surface_r4", "memrack/config_surface.py")
    home = _fresh_home(tmp_path)
    (home / "memrack").mkdir(parents=True, exist_ok=True)
    (home / "memrack" / "config.json").write_text(
        json.dumps({"dock_want": "fakeprov", "search_top_k": 8}),
        encoding="utf-8")
    seen = {}

    def fake_json_write(path, data, *args, **kwargs):
        seen["path"] = pathlib.Path(path)
        seen["data"] = data
        seen["kwargs"] = kwargs
        seen["path"].write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(cs, "_host_json_writer", lambda: fake_json_write)
    report = cs._consume_dock_want(
        {"dock_want": "fakeprov"}, "fakeprov",
        {"provider": "fakeprov", "status": "draft-docked"},
        hermes_home=str(home))
    assert report.get("consumed") is True, report
    stores = report.get("store_write") or {}
    assert seen["data"].get("dock_want") is None, seen["data"]
    assert (seen["data"].get("dock_want_last") or {}).get("provider") == "fakeprov"
    assert seen["kwargs"].get("mode") == 0o600, seen["kwargs"]
    assert "json" in stores, stores


def test_flat_seam_writer_falls_back_without_host_helpers(monkeypatch, tmp_path):
    cs = _spec_load("memrack_config_surface_r4b", "memrack/config_surface.py")
    home = _fresh_home(tmp_path)
    _seed_dock_want(home, "fakeprov")
    monkeypatch.setattr(cs, "_host_json_writer", lambda: None)
    monkeypatch.setattr(cs, "_host_yaml_writer", lambda home_: None)
    spy = _ProbeSpyDocking()
    report = cs.dock_want_hook({"dock_want": "fakeprov"},
                               hermes_home=str(home), docking=spy)
    assert report.get("status") == "draft-docked", report
    assert len(spy.probes) == 1, spy.probes
    flat = json.loads((home / "memrack" / "config.json").read_text(
        encoding="utf-8-sig"))
    assert flat.get("dock_want") is None, flat
    assert (flat.get("dock_want_last") or {}).get("provider") == "fakeprov", flat
    block = (yaml.safe_load((home / "config.yaml").read_text("utf-8-sig"))
             or {}).get("plugins", {}).get("memrack", {})
    assert block.get("dock_want") is None, block
    assert (block.get("dock_want_last") or {}).get("provider") == "fakeprov", block
    for path in (home / "config.yaml", home / "memrack" / "config.json"):
        assert path.stat().st_mode & 0o777 == 0o600, path


def test_machine_store_switch_prefers_the_host_writers(monkeypatch, tmp_path):
    docking = _spec_load("memrack_docking_r4c", "memrack/docking.py")
    home = _fresh_home(tmp_path)
    (home / "config.yaml").write_text(
        "plugins:\n  memrack:\n    search_top_k: 3\n", encoding="utf-8")
    (home / "memrack").mkdir(parents=True, exist_ok=True)
    seen = {}

    def fake_yaml_save(config, *args, **kwargs):
        seen["yaml"] = config
        (home / "config.yaml").write_text(
            yaml.dump(config, sort_keys=False), encoding="utf-8")

    def fake_json_write(path, data, *args, **kwargs):
        seen["json"] = data
        pathlib.Path(path).write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(docking, "_host_yaml_writer",
                        lambda home_, **kw: fake_yaml_save)
    monkeypatch.setattr(docking, "_host_json_writer", lambda: fake_json_write)
    out = docking.store_switch("fakesvc", False, hermes_home=str(home))
    assert "error" not in out, out
    block = seen["yaml"]["plugins"]["memrack"]
    assert block["fakesvc_in_rack"] is False, block
    assert block["search_top_k"] == 3, "other keys of the block survive"
    assert seen["json"]["fakesvc_in_rack"] is False, seen["json"]
    yaml_block = (yaml.safe_load((home / "config.yaml").read_text("utf-8-sig"))
                  or {}).get("plugins", {}).get("memrack", {})
    assert yaml_block.get("fakesvc_in_rack") is False, yaml_block

