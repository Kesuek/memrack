"""proof batch: guide-only proof results + the card-hierarchy lesson."""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr


class DynBackend:
    """A config-gated backend: card_modes() = () without a root (the honest case)."""
    name = "dyn"
    best_for = "x"
    limits = "y"
    writeable = False
    manifest = {}

    def __init__(self, root=None):
        self.root = root

    def card_modes(self):
        return ("fts",) if self.root else ()

    def initialize(self, session_id="", hermes_home=None, provider_config=None, **k): pass
    def on_turn(self, *a, **k): pass
    def on_memory_write(self, *a, **k): pass
    def on_session_end(self, *a, **k): pass
    def prefetch(self, *a, **k): return ""
    def system_prompt_block(self): return ""
    def shutdown(self): pass
    def export(self): return {"name": self.name}


def test_manifest_never_overrides_the_honest_card():
    """Proof-point 4: a manifest claiming [fts] on a rootless dyn backend must NOT
    arm the card — the env-truth wins; the divergence is VISIBLE ('declared')."""
    b = DynBackend(root=None)
    b.manifest = {"name": "dyn", "module": "dyn.py", "search": ["fts"]}
    card = mr._card(b)
    assert card["modes"] == []            # NOTHING promised (honest!)
    assert card["declared"] == ["fts"]    # the excess stays visible
    # with root: the delivery wins and arms exactly the mode
    b2 = DynBackend(root="/tmp")
    b2.manifest = {"name": "dyn", "module": "dyn.py", "search": ["fts"]}
    card2 = mr._card(b2)
    assert card2["modes"] == ["fts"] and card2["declared"] == []


def test_contrib_dirsearch_conforms_with_manifest(tmp_path):
    """The worked example: contrib/dirsearch.py + its manifest — runner-level flow."""
    import importlib.util as iu
    spec = iu.spec_from_file_location("memrack_conf", _ROOT / "memrack" / "conformance.py")
    conf = iu.module_from_spec(spec)
    spec.loader.exec_module(conf)
    tspec = iu.spec_from_file_location("memrack_ds", _ROOT / "contrib" / "dirsearch.py")
    mod = iu.module_from_spec(tspec)
    tspec.loader.exec_module(mod)
    obj = mod.export_backend()
    info = conf._apply_sidecar_manifest(obj, _ROOT / "contrib" / "dirsearch.py")
    assert info["manifest"] == "dirsearch.yaml"
    assert info["card_source"] == "manifest(+code-verify)"
    assert info["declared_not_armed"] == ["fts"]   # without root: claim stays un-armed (honest)
    report = conf.check_backend(obj, tmp_home=tmp_path)
    assert report["ok"], report
