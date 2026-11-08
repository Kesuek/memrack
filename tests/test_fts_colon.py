"""T-029 bug 1 — a raw ':' in a query must not silently blind the FTS search.

External review 2026-11-08 (reproduced): every query carrying a colon rode
into fts5 MATCH as raw advanced syntax ('10:30' = 'no such column: 30'), the
quoted phrase fallback re-entered the SAME passthrough, and both tiers failed
— 0 hits, no error surfaced. Pinned here: (a,b) plain colon queries (times,
URLs) find the stored fact, (c) a REAL column filter (subject:Relay) keeps
riding as passthrough, (d) the provider fan-out returns the hit with an empty
engine_errors list, (e/f) the SDK mirror behaves the same way.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr

ENGINE = importlib.util.spec_from_file_location(
    "memrack_base_db", pathlib.Path(_ROOT / "backends" / "base_db.py"))
base_db = importlib.util.module_from_spec(ENGINE)
ENGINE.loader.exec_module(base_db)

SDK = importlib.util.spec_from_file_location(
    "memrack_sdk", pathlib.Path(_ROOT / "memrack" / "sdk.py"))
sdk = importlib.util.module_from_spec(SDK)
SDK.loader.exec_module(sdk)


@pytest.fixture()
def eng(tmp_path):
    e = base_db.BaseDbBackend()
    e.initialize(session_id="S1", hermes_home=str(tmp_path))
    return e


def test_time_query_finds_fact_fts(eng):
    eng.write("explicit", "Meeting startet 10:30 Uhr im Relay-Raum")
    hits = eng.search("10:30 Uhr", "fts", 8)
    assert len(hits) == 1, "a colon in the query must not blind the fts search"
    assert "10:30" in hits[0]["content"]


def test_url_query_finds_fact_fts(eng):
    eng.write("explicit", "Docs live unter http://x.example/deploy")
    hits = eng.search("http://x", "fts", 8)
    assert len(hits) == 1, "a URL colon must not blind the fts search"


def test_real_column_filter_still_passthrough(eng):
    eng.write_pointer("Relay", "~/wiki/main/entities/relay.md")
    eng.write_pointer("Alpha", "~/wiki/main/entities/alpha.md")
    hits = eng.search("subject:Relay", "pointers", 8)
    assert len(hits) == 1, "advanced passthrough must keep working for real columns"
    assert hits[0]["content"].startswith("Relay -> ")


def test_safe_match_passthrough_only_real_columns():
    sm = base_db.BaseDbBackend._safe_match
    assert sm("subject:Relay", ("subject", "location")) == "subject:Relay"
    assert sm("content:test", ("content",)) == "content:test"
    frag = sm("10:30 Uhr", ("content",))
    assert "10:30" not in frag, "no raw fts5 syntax may ride from a plain query"
    frag = sm("http://x", ("content",))
    assert "http:" not in frag


def test_provider_colon_query_clean(tmp_path):
    prov = mr._MemrackBase(config={})
    prov.initialize(session_id="T029", hermes_home=str(tmp_path))
    prov.handle_tool_call("memrack_write", {"content": "Meeting startet 10:30 Uhr im Relay-Raum"})
    out = json.loads(prov.handle_tool_call("memrack_search", {"query": "10:30 Uhr"}))
    assert out["results"], "the seeded fact must be found through the fan-out"
    assert out["engine_errors"] == [], "the normal case carries no engine errors"


def test_sdk_time_query_not_passthrough():
    frag = sdk.fts_match_tokens("10:30 Uhr")
    assert "10:30" not in frag, "no raw fts5 syntax may ride from a plain query"
    assert frag == '"10" "30" "Uhr"'   # sdk quote path: implicit AND (existing law)


def test_sdk_real_column_passthrough():
    assert sdk.fts_match_tokens("subject:Relay", columns=("subject", "location")) == "subject:Relay"
    frag = sdk.fts_match_tokens("10:30 Uhr", columns=("subject", "location"))
    assert "10:30" not in frag
    assert 'subject:("10" "30" "Uhr")' in frag
