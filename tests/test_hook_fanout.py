"""Hook fan-out over the whole rack (T-031, decision (a)): the hooks prefetch,
on_session_end, on_pre_compress, on_session_switch and system_prompt_block run
at EVERY active backend, one engine's failure stays isolated, and the
aggregation keeps the single-result forms byte-identical (no breaking)."""
import importlib.util
import itertools
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr


def _backend_src(name, *, end="", spb="", pre=None, switch=None, fail_hook=""):
    """Builder for a docked stub backend: `end`/`spb` are literal return values
    ("" → falsy, dropped by the merge), `pre`/`switch` the rescue/switch returns,
    `fail_hook` names one hook that raises RuntimeError("boom")."""
    fail_body = 'raise RuntimeError("boom")'
    body = """
class Be:
    name = %r
    write_types = ("explicit", "curated")

    def __init__(self):
        self.calls = []

    def initialize(self, session_id="", hermes_home=None, **kwargs):
        self.calls.append(("initialize", session_id))

    def on_turn(self, user, assistant, *, session_id=""):
        self.calls.append(("on_turn", user, assistant))

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        self.calls.append(("on_memory_write", action, target))

    def on_session_end(self, messages, *, session_id=""):
        self.calls.append(("on_session_end", messages, session_id))
        if %r == "end":
            %s
        return %r

    def on_pre_compress(self, messages, **kwargs):
        self.calls.append(("on_pre_compress", messages))
        if %r == "pre":
            %s
        return %r

    def on_session_switch(self, new_session_id=None, **kwargs):
        self.calls.append(("on_session_switch", new_session_id))
        if %r == "switch":
            %s
        return %r

    def prefetch(self, query, *, session_id=""):
        self.calls.append(("prefetch", query, session_id))
        if %r == "prefetch":
            %s
        return "PREF-%s"

    def system_prompt_block(self):
        self.calls.append(("system_prompt_block",))
        if %r == "spb":
            %s
        return %r

    def shutdown(self):
        self.calls.append(("shutdown",))

    def export(self):
        return {"name": self.name}


def export_backend():
    return Be()
""" % (name, fail_hook, fail_body, end,
       fail_hook, fail_body, pre,
       fail_hook, fail_body, switch,
       fail_hook, fail_body, name,
       fail_hook, fail_body, spb)
    return body


NAKED_SRC = """
class Naked:
    name = "naked"
    write_types = ("explicit", "curated")

    def __init__(self):
        self.calls = []

    def initialize(self, session_id="", hermes_home=None, **kwargs):
        self.calls.append(("initialize", session_id))

    def on_turn(self, user, assistant, *, session_id=""):
        self.calls.append(("on_turn", user, assistant))

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        self.calls.append(("on_memory_write", action, target))

    def on_session_end(self, messages, *, session_id=""):
        self.calls.append(("on_session_end", messages, session_id))

    def prefetch(self, query, *, session_id=""):
        self.calls.append(("prefetch", query, session_id))
        return ""

    def system_prompt_block(self):
        self.calls.append(("system_prompt_block",))
        return ""

    def shutdown(self):
        self.calls.append(("shutdown",))

    def export(self):
        return {"name": self.name}


def export_backend():
    return Naked()
"""   # NO on_pre_compress / on_session_switch — legally optional hooks


def _dock(tmp_path, name, body):
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    dock.mkdir(parents=True, exist_ok=True)
    (dock / name).write_text(body, encoding="utf-8")


_HOME_SEQ = itertools.count()


def _provider(tmp_path, sources, forced="alpha"):
    """One provider per FRESH home dir: re-docking the same file names in one
    dir would hit Python's mtime+size bytecode cache (same-length sources, same
    second) and resurrect previous backends — a test-isolation landmine."""
    home = tmp_path / ("home-%d" % next(_HOME_SEQ))
    for fname, src in sources:
        _dock(home, fname, src)
    prov = mr._MemrackBase(config={"backend": forced, "enabled_backends": ["base-db"]})
    prov.initialize(session_id="S1", hermes_home=str(home))
    return prov


ALL_HOOKS = ("on_session_end", "on_pre_compress", "on_session_switch",
             "prefetch", "system_prompt_block")


def test_every_hook_reaches_every_backend(tmp_path):
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha")),
        ("beta.py", _backend_src("beta")),
    ])
    assert set(prov._rack) == {"alpha", "base-db", "beta"}
    prov.prefetch("q")
    prov.system_prompt_block()
    prov.on_session_end()
    prov.on_pre_compress([])
    prov.on_session_switch("NEW")
    for bname in ("alpha", "beta"):
        seen = [c[0] for c in prov._rack[bname].calls]
        for hook in ALL_HOOKS:
            assert hook in seen, "%s missed %s" % (bname, hook)
    # arguments arrive intact (session_id flows through)
    assert ("prefetch", "q", "S1") in prov._rack["beta"].calls
    assert ("on_session_end", None, "S1") in prov._rack["alpha"].calls


def test_prefetch_merges_nonempty_parts(tmp_path):
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha")),
        ("beta.py", _backend_src("beta")),
    ])
    assert prov.prefetch("wer") == "PREF-alpha\n\nPREF-beta"
    # empty parts drop out; a single engine still answers a plain str
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha")),
        ("beta.py", _backend_src("beta", fail_hook="prefetch")),
    ])
    assert prov.prefetch("wer") == "PREF-alpha"
    assert isinstance(prov.prefetch("wer"), str)


def test_system_prompt_block_concats_dedupes_nonempty(tmp_path):
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha", spb="SAME-BLOCK")),
        ("beta.py", _backend_src("beta", spb="SAME-BLOCK")),
    ])
    spb = prov.system_prompt_block()
    assert spb.startswith("SAME-BLOCK")
    assert spb.count("SAME-BLOCK") == 1            # dedupe across backends
    assert "memrack rack (v" in spb                # the routing-law card stays
    # only alpha ships a block → it wins alone
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha", spb="ONLY-ALPHA")),
        ("beta.py", _backend_src("beta", spb="")),
    ])
    assert "ONLY-ALPHA\n\nmemrack rack (v" in prov.system_prompt_block()


def test_single_result_keeps_form(tmp_path):
    """>1 backends but one result → the pre-fanout form survives (kein Breaking)."""
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha", end=None)),
        ("beta.py", _backend_src("beta", end={"who": "beta"}, pre=None, switch=None)),
    ])
    assert prov.on_session_end() == {"who": "beta"}
    assert prov.on_pre_compress([1, 2]) is None
    assert prov.on_session_switch("NEW") is None


def test_multi_result_packs_per_backend_dict(tmp_path):
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha", end={"who": "alpha"},
                                  pre={"pc": "alpha"}, switch="alpha-on")),
        ("beta.py", _backend_src("beta", end={"who": "beta"},
                                 pre={"pc": "beta"}, switch="beta-on")),
    ])
    assert prov.on_session_end() == {"alpha": {"who": "alpha"}, "beta": {"who": "beta"}}
    assert prov.on_pre_compress([]) == {"alpha": {"pc": "alpha"}, "beta": {"pc": "beta"}}
    assert prov.on_session_switch("NEW") == {"alpha": "alpha-on", "beta": "beta-on"}


def test_error_isolated_rest_still_runs(tmp_path, caplog):
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha", end={"who": "alpha"})),
        ("beta.py", _backend_src("beta", end={"who": "beta"})),
        ("gamma.py", _backend_src("gamma", fail_hook="end")),
    ])
    out = prov.on_session_end()
    assert out["alpha"] == {"who": "alpha"}
    assert out["beta"] == {"who": "beta"}
    assert [e["backend"] for e in out["engine_errors"]] == ["gamma"]
    assert "boom" in out["engine_errors"][0]["error"]
    assert "memrack backend gamma hook on_session_end failed" in caplog.text

    # single result + an error elsewhere: form preserved, error still logged
    caplog.clear()
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha", end={"who": "alpha"})),
        ("beta.py", _backend_src("beta", end=None, fail_hook="end")),
    ])
    assert prov.on_session_end() == {"who": "alpha"}
    assert "boom" in caplog.text

    # prefetch: a failing engine never kills the other strings
    caplog.clear()
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha")),
        ("beta.py", _backend_src("beta", fail_hook="prefetch")),
    ])
    assert prov.prefetch("wer") == "PREF-alpha"
    assert "boom" in caplog.text


def test_optional_hooks_missing_are_silent(tmp_path, caplog):
    """A backend without on_pre_compress/on_session_switch: no error, no warn spam."""
    prov = _provider(tmp_path, [
        ("alpha.py", _backend_src("alpha")),
        ("naked.py", NAKED_SRC),
    ])
    assert prov.on_pre_compress([]) is None
    assert prov.on_session_switch("NEW") is None
    assert prov.prefetch("wer") == "PREF-alpha"
    assert "naked" not in caplog.text                     # no warn spam
    naked = prov._rack["naked"]
    assert ("on_pre_compress", []) not in naked.calls     # and no phantom call
    assert ("on_session_switch", "NEW") not in naked.calls


def test_single_backend_forms_unchanged(tmp_path):
    """Solo stub rack: byte-identical to the pre-fanout contracts."""
    prov = _provider(tmp_path, [("alpha.py", _backend_src("alpha", spb="ALPHA-SPB"))])
    assert set(prov._rack) == {"alpha", "base-db"}
    assert prov.prefetch("wer") == "PREF-alpha"
    spb = prov.system_prompt_block()
    assert spb.startswith("ALPHA-SPB")
    assert "memrack rack (v" in spb
    prov.shutdown()
