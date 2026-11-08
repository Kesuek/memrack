"""External-review round-3 fixes (Fix-Queue C) — small-behavior pins:

1.  fresh stores: `facts.id` AUTOINCREMENT (no rowid reuse) + every delete
    prunes `facts_vec` (rowid = fact id) — works on existing stores too;
2.  remove respects `previous_content`, exact match first, substring fallback
    with an honest removed-report; replace/patch respect previous_content;
3.  vec hits carry the fact id (semantic hits burn heat), the engine derives
    its embedding dimension from a model probe (honest degrade on mismatch),
    and the backfill batch is configurable (`vec_backfill_batch`) with a
    visible backlog count instead of silent incompleteness;
4.  `memrack_import` splits §-sections CRLF-safe and degrades (log + proceed)
    when a forced foreign backend ignores `target_hint`;
5.  the base-db floor mounts with >=1 non-base entry (not >=2);
6.  `initialize` resets `_gaps` and `_force`;
7.  shared-connection reads are lock-guarded; search touches commit ONCE;
8.  vocabulary: write_types explicit|curated only, legacy "curation" input
    tolerated and stored as curated;
9.  tool schemas carry no `"default": None` on string properties.
"""
import importlib.util
import json
import pathlib
import sqlite3
import sys

import pytest  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

spec = importlib.util.spec_from_file_location("memrack", _ROOT / "__init__.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
sys.modules["memrack"] = mr
import memrack as mr  # noqa: E402


def _load_mod(relpath, name):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE_DB = _load_mod("backends/base_db.py", "memrack_base_db_review3")
WikiMod = _load_mod("backends/wiki.py", "memrack_wiki_review3")


def _engine(tmp_path):
    prov = mr._MemrackBase()
    prov.initialize(session_id="R3", hermes_home=str(tmp_path))
    return prov._active


def _vec_engine(tmp_path, monkeypatch, dim=4, batch=None, sid="R3V1"):
    """base-db engine with the vec engine FORCED on (probe True, fake embeddings)."""
    eng = BASE_DB.BaseDbBackend()
    monkeypatch.setattr(eng, "_vec_probe", lambda: True)

    def _embed(texts, dim=dim):
        return [[float(len(t)) % 7 / 7, 0.5, 0.25] + [0.0] * (dim - 3) for t in texts]

    monkeypatch.setattr(eng, "_embed", _embed)
    cfg = {"vec_backfill_batch": str(batch)} if batch else {}
    eng.initialize(session_id=sid, hermes_home=str(tmp_path), provider_config=cfg)
    return eng


# ── 5) floor: base-db mounts with a SINGLE non-base entry ───────────────────

def test_floor_mounts_with_single_nonbase_entry(tmp_path):
    prov = mr._MemrackBase(config={"enabled_backends": "wiki"})
    prov.initialize(session_id="R3", hermes_home=str(tmp_path))
    assert "base-db" in prov._rack, "one non-base entry must mount the floor"
    assert prov._active is prov._rack["base-db"]


# ── 6) initialize resets _gaps and _force ────────────────────────────────────

def test_initialize_resets_gaps_and_force(tmp_path):
    prov = mr._MemrackBase()
    prov.initialize(session_id="R3a", hermes_home=str(tmp_path))
    prov._gaps.append({"backend": "stale", "error": "stale gap"})
    prov._force = True
    prov.initialize(session_id="R3b", hermes_home=str(tmp_path))
    assert all(g.get("backend") != "stale" for g in prov._gaps), prov._gaps
    assert prov._force is False


# ── 9) tool schemas: no `"default": None` on string properties ──────────────

def test_tool_schemas_have_no_null_default(tmp_path):
    prov = mr._MemrackBase()
    prov.initialize(session_id="R3", hermes_home=str(tmp_path))
    checked = 0
    for schema in prov.get_tool_schemas():
        params = (schema.get("function") or {}).get("parameters") or {}
        for prop in (params.get("properties") or {}).values():
            if "default" in prop:
                checked += 1
                assert prop["default"] is not None, schema
    assert checked > 0, "the probe must actually see the tool properties"


# ── 4) import: CRLF-safe split + honest degrade without target_hint ─────────

def test_import_crlf_sections(tmp_path):
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "MEMORY.md").write_text(
        "first legacy entry about caching\r\n\u00a7\r\nsecond legacy entry about routing\r\n",
        encoding="utf-8")
    prov = mr._MemrackBase()
    prov.initialize(session_id="IMP", hermes_home=str(tmp_path))
    out = json.loads(prov.handle_tool_call("memrack_import", {}))
    assert out["imported"] == 2 and out["duplicates"] == 0, out


_HINTLESS = """
class Hintless:
    name = "hintless"
    best_for = "import probe"
    limits = "write ignores target_hint"
    writeable = True

    def __init__(self):
        self.writes = []

    def initialize(self, session_id="", hermes_home=None, **kwargs):
        pass

    def on_turn(self, user, assistant, *, session_id=""): return None

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        return None

    def on_session_end(self, messages, *, session_id=""): return None

    def prefetch(self, query, *, session_id=""): return ""

    def system_prompt_block(self): return ""

    def shutdown(self): pass

    def export(self): return {"name": self.name}

    def write(self, kind, content, tags=None, session_id=""):
        self.writes.append(content)
        return {"status": "stored", "target": "hintless:%d" % len(self.writes)}


def export_backend():
    return Hintless()
"""


def _hintless_provider(tmp_path):
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    dock.mkdir(parents=True)
    (dock / "hintless.py").write_text(_HINTLESS, encoding="utf-8")
    prov = mr._MemrackBase(config={"backend": "hintless"})
    prov.initialize(session_id="IMP2", hermes_home=str(tmp_path))
    return prov


def test_import_degrades_when_backend_ignores_target_hint(tmp_path):
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("entry one about caching\n\u00a7\nentry two about routing",
                                   encoding="utf-8")
    prov = _hintless_provider(tmp_path)
    out = json.loads(prov.handle_tool_call("memrack_import", {}))
    assert out["imported"] == 2, out                       # proceed, not TypeError
    assert "ignores target_hint" in out.get("note", "")    # honest report
    hintless = prov._rack["hintless"]
    assert len(hintless.writes) == 2


# ── 2) remove: exact first, substring fallback, honest report ───────────────

def test_remove_exact_then_substring_honest_report(tmp_path):
    eng = _engine(tmp_path)
    eng.on_memory_write("add", "memory", "alpha beta")
    eng.on_memory_write("add", "memory", "alpha-beta-gamma")
    out = eng.on_memory_write("remove", "memory", "alpha beta")
    assert out["match"] == "exact" and out["removed"] == 1
    rows = [r[0] for r in eng._db.execute("SELECT content FROM facts").fetchall()]
    assert rows == ["alpha-beta-gamma"]
    out2 = eng.on_memory_write("remove", "memory", "gamma")
    assert out2["match"] == "substring" and out2["removed"] == 1
    assert any("alpha-beta-gamma" in r["content"] for r in out2["rows"])
    out3 = eng.on_memory_write("remove", "memory", "never known anywhere")
    assert out3["status"] == "not_found" and out3["removed"] == 0
    assert eng._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_remove_respects_previous_content(tmp_path):
    """The runtime mirrors remove with the FULL previous entry in metadata (content may be empty)."""
    eng = _engine(tmp_path)
    eng.on_memory_write("add", "memory", "the exact previous entry")
    out = eng.on_memory_write(
        "remove", "memory", "", metadata={"previous_content": "the exact previous entry"})
    assert out["removed"] == 1, out
    assert eng._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_replace_and_patch_respect_previous_content(tmp_path):
    eng = _engine(tmp_path)
    eng.on_memory_write("add", "memory", "old entry text here")
    eng.on_memory_write("replace", "memory", "new entry text",
                        metadata={"previous_content": "old entry text here"})
    rows = [r[0] for r in eng._db.execute("SELECT content FROM facts ORDER BY id").fetchall()]
    assert rows == ["new entry text"], "the stale previous entry must not survive a replace"
    eng.on_memory_write("add", "memory", "second old text")
    eng.on_memory_write("patch", "memory", "second new text",
                        metadata={"previous_content": "second old text"})
    rows = [r[0] for r in eng._db.execute("SELECT content FROM facts ORDER BY id").fetchall()]
    assert rows == ["new entry text", "second new text"], rows


# ── 1) AUTOINCREMENT + facts_vec prune at delete ─────────────────────────────

def test_remove_released_rowid_not_reused_on_fresh_store(tmp_path):
    eng = _engine(tmp_path)
    r1 = eng.write("explicit", "first content ever stored")
    eng.on_memory_write("remove", "memory", "first content ever stored")
    r2 = eng.write("explicit", "second content ever stored")
    assert r2["id"] > r1["id"], "a deleted id must not be reused on fresh stores"


def test_remove_prunes_facts_vec(tmp_path, monkeypatch):
    eng = _vec_engine(tmp_path, monkeypatch)
    eng.write("explicit", "the prune target fact text")
    eng.search("the prune target", "vec", 8)
    assert eng._db.execute("SELECT COUNT(*) FROM facts_vec").fetchone()[0] == 1
    out = eng.on_memory_write("remove", "memory", "the prune target fact text")
    assert out["removed"] == 1
    assert eng._db.execute("SELECT COUNT(*) FROM facts_vec").fetchone()[0] == 0


# ── 3) vec hits carry the id, dimension probe, backfill batch ───────────────

def test_vec_hits_carry_fact_id(tmp_path, monkeypatch):
    eng = _vec_engine(tmp_path, monkeypatch)
    r = eng.write("explicit", "semantic heat probe fact here")
    hits = eng.search("semantic heat probe", "vec", 8)
    assert hits and hits[0]["id"] == r["id"], "vec hits must carry the fact rowid for touch"


def test_semantic_hits_burn_heat(tmp_path):
    eng = _engine(tmp_path)
    eng.on_memory_write("add", "memory", "fusion heat probe entry")
    hits = eng.search("fusion heat", "fts", 8)
    merged = [{**h, "id": (h.get("id") or r["id"]), "backend": "base-db"}  # vec-shaped hit
              for h, r in zip(hits, [eng._db.execute("SELECT id FROM facts").fetchone()])]
    prov = mr._MemrackBase()
    prov.initialize(session_id="H", hermes_home=str(tmp_path))
    prov._rack = {"base-db": eng}
    prov._touch_facts(merged)
    after = eng._db.execute("SELECT hits FROM facts").fetchone()[0]
    assert after >= 1, "a hit carrying the fact id must increment heat"


def test_vec_backfill_batch_and_backlog_field(tmp_path, monkeypatch):
    eng = _vec_engine(tmp_path, monkeypatch, batch="1")
    for text in ("first backlog fact alpha", "second backlog fact beta",
                 "third backlog fact gamma"):
        eng.write("explicit", text)
    hits = eng.search("first backlog fact", "vec", 8)
    assert hits and hits[0]["_vec_backlog"] == 2, "remaining un-embedded rows must be visible"
    assert eng._db.execute("SELECT COUNT(*) FROM facts_vec").fetchone()[0] == 1
    hits2 = eng.search("second backlog fact", "vec", 8)
    assert hits2[0]["_vec_backlog"] == 1


def test_vec_dimension_probe_sizes_new_table(tmp_path, monkeypatch):
    eng = _vec_engine(tmp_path, monkeypatch, dim=8)
    eng.write("explicit", "dimension probe fact content")
    hits = eng.search("dimension probe", "vec", 8)     # insert must succeed at dim 8
    assert hits
    assert eng._db.execute("SELECT vec_length(embedding) FROM facts_vec LIMIT 1"
                           ).fetchone()[0] == 8


def test_vec_dimension_mismatch_degrades_honestly(tmp_path, monkeypatch):
    eng = _vec_engine(tmp_path, monkeypatch, dim=4)
    eng.write("explicit", "first stored fact here")
    eng.search("first stored fact", "vec", 8)          # indexed at dim 4
    eng.shutdown()
    eng2 = BASE_DB.BaseDbBackend()
    monkeypatch.setattr(eng2, "_vec_probe", lambda: True)
    monkeypatch.setattr(eng2, "_embed", lambda texts: [[0.1] * 6 for _ in texts])
    eng2.initialize(session_id="R3V2", hermes_home=str(tmp_path))
    assert eng2._vec_ok is False
    assert "dimension mismatch" in eng2._vec_gap
    assert "vec" not in eng2.card_modes()
    with pytest.raises(ValueError):
        eng2.search("anything", "vec", 8)


def test_search_surfaces_vec_backlog(tmp_path):
    dock = tmp_path / "plugins" / "memrack" / "backends.d"
    dock.mkdir(parents=True)
    (dock / "notary.py").write_text(
        "class Notary:\n"
        "    name = 'notary'\n"
        "    best_for = 'backlog probe'\n"
        "    limits = '-'\n"
        "    search_modes = ('fts',)\n"
        "    def initialize(self, session_id='', hermes_home=None, **k): pass\n"
        "    def on_turn(self, *a, **k): return None\n"
        "    def on_memory_write(self, *a, **k): return None\n"
        "    def on_session_end(self, *a, **k): return None\n"
        "    def prefetch(self, *a, **k): return ''\n"
        "    def system_prompt_block(self): return ''\n"
        "    def shutdown(self): pass\n"
        "    def export(self): return {'name': self.name}\n"
        "    def touch(self, ids): pass\n"
        "    def search(self, query, mode, k):\n"
        "        return [{'backend': 'notary', 'kind': 'fact', 'rank': 1, 'id': 42,\n"
        "                 'target': 'notary:1', 'content': query, 'ts': 'x',\n"
        "                 '_vec_backlog': 2}]\n"
        "def export_backend():\n"
        "    return Notary()\n", encoding="utf-8")
    prov = mr._MemrackBase()
    prov.initialize(session_id="NB", hermes_home=str(tmp_path))
    out = json.loads(prov.handle_tool_call("memrack_search", {"query": "anything"}))
    assert out["vec_backlog"] == 2, "an engine-reported backlog must surface in the answer"
    assert all("_vec_backlog" not in h for h in out["results"])


# ── 10) read-path touches: ONE commit per search ─────────────────────────────

def test_search_touch_is_one_commit(tmp_path):
    eng = _engine(tmp_path)
    eng.on_memory_write("add", "memory", "commit probe entry one")
    eng.on_memory_write("add", "memory", "commit probe entry two")

    class _CountConn:
        def __init__(self, real):
            self._real = real
            self.commits = 0

        def commit(self):
            self.commits += 1
            return self._real.commit()

        def __getattr__(self, name):
            return getattr(self._real, name)

    eng._db = _CountConn(eng._db)
    prov = mr._MemrackBase()
    prov.initialize(session_id="TC", hermes_home=str(tmp_path))
    prov._rack["base-db"] = eng
    prov.handle_tool_call("memrack_search", {"query": "commit probe", "mode": "fts"})
    assert eng._db.commits == 1, "one search = one touch transaction (no per-row commit)"
    hits = [r[0] for r in eng._db.execute("SELECT hits FROM facts").fetchall()]
    assert all(h >= 1 for h in hits), "every returned fact row is burned in that one commit"


# ── 8) vocabulary: write_types explicit|curated, curation tolerated ─────────

def test_write_types_vocab_curated_replaces_curation():
    assert BASE_DB.BaseDbBackend.write_types == ("explicit", "curated")
    assert WikiMod.WikiBackend.write_types == ("explicit", "curated")


def test_legacy_curation_kind_tolerated_as_curated(tmp_path):
    prov = mr._MemrackBase()
    prov.initialize(session_id="V", hermes_home=str(tmp_path))
    out = json.loads(prov.handle_tool_call(
        "memrack_write", {"content": "legacy vocabulary line here", "kind": "curation"}))
    assert out.get("status") == "stored", out
    kind = prov._active._db.execute("SELECT kind FROM facts").fetchone()[0]
    assert kind == "curated", "legacy input must be stored under the curated vocabulary"
