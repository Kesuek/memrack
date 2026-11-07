"""conformance harness + sdk base — the proof: template conforms, lies don't."""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr_mod)
sys.modules["memrack"] = mr_mod

SDK = importlib.util.spec_from_file_location("memrack_sdk", _ROOT / "memrack" / "sdk.py")
sdk = importlib.util.module_from_spec(SDK)
SDK.loader.exec_module(sdk)

CONF = importlib.util.spec_from_file_location("memrack_conformance", _ROOT / "memrack" / "conformance.py")
conf = importlib.util.module_from_spec(CONF)
CONF.loader.exec_module(conf)

TPL = importlib.util.spec_from_file_location("memrack_template", pathlib.Path(_ROOT / "backends" / "_template.py"))
tpl = importlib.util.module_from_spec(TPL)
TPL.loader.exec_module(tpl)


def test_template_backend_passes_conformance(tmp_path):
    obj = tpl.export_backend()
    report = conf.check_backend(obj, tmp_home=tmp_path)
    assert report["ok"], report
    # smoke proved write + idempotence + hit-shape
    assert obj.write("explicit", "guide-proof")["status"] == "stored"


def test_card_lie_fails_conformance(tmp_path):
    class Lying(sdk.MemrackBackend):
        name = "liar"
        search_modes = ("fts", "vec")      # vec claimed WITHOUT an engine

    report = conf.check_backend(Lying(), tmp_home=tmp_path)
    assert not report["ok"]
    assert any(e["error"].startswith("claims search modes") for e in report["errors"])


def test_missing_card_is_a_warning_not_error(tmp_path):
    class Naked(sdk.MemrackBackend):
        name = "naked"
        def initialize(self, *a, **k): pass

    report = conf.check_backend(Naked(), tmp_home=tmp_path)
    assert report["ok"]                        # structure fine
    assert len(report["warnings"]) >= 2        # best_for/limits empty = warnings


def test_fts_match_tokens_quotes_hyphens():
    out = sdk.fts_match_tokens("Relay LXC-Hit")
    assert '"LXC"' in out and '"Hit"' in out    # the hyphen-split token is safe
    assert out.count('"') % 2 == 0
    col = sdk.fts_match_tokens("Relay x", columns=["subject", "location"])
    assert "subject:(" in col and "location:(" in col


def test_registry_accepts_sdk_base_instances(tmp_path):
    """The SDK base class duck-passes the registry checks (no duplicate rule set)."""
    obj = tpl.export_backend()
    obj2 = conf.check_backend(obj, tmp_home=tmp_path)
    validated = __import__("memrack")._validate_backend_for_registry if False else None
    # the plugin's ensure_backend = same structure without the SDK import:
    mr = sys.modules["memrack"]
    cleaned = mr.ensure_backend(obj, origin="template")
    assert cleaned.name == "template"
    assert obj2["ok"]
