"""External-review round-3 fixes (Fix-Queue C) — small-behavior pins:

1.  fresh stores: `facts.id` AUTOINCREMENT (no rowid reuse) + every delete
    prunes `facts_vec` (rowid = fact id) — works on existing stores too;
2.  remove respects `previous_content`, exact match first, substring fallback
    with an honest removed-report; replace/patch respect previous_content;
3.  vec hits carry the chunk id (doc hits burn the vec backend's OWN per-chunk
    heat — the ledger heat is a base-db matter only, the harvest is ENDED),
    the engine derives its embedding dimension from a model probe (honest
    degrade on mismatch), and the ingest embed-batch is configurable
    (`vec_backfill_batch` = the embed batch per add_source call);
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


def _fake_vec(tmp_path, monkeypatch, dim=4, batch=None, sid="R3V1"):
    """The new VecBackend with the probe FORCED on and fake embeddings —
    the store laws under test, never the embedder (the harvest is ENDED;
    document ingest runs via add_source into the store's OWN corpus)."""
    vecspec = importlib.util.spec_from_file_location(
        "memrack_backends_vec", pathlib.Path(_ROOT / "backends" / "vec.py"))
    VEC = importlib.util.module_from_spec(vecspec)
    vecspec.loader.exec_module(VEC)
    vb = VEC.VecBackend()
    monkeypatch.setattr(vb, "_probe", lambda: True)

    def _embed(texts, dim=dim):
        return [[float(len(t)) % 7 / 7, 0.5, 0.25] + [0.0] * (dim - 3) for t in texts]

    monkeypatch.setattr(vb, "_embed", _embed)
    cfg = {"vec_backfill_batch": str(batch)} if batch else {}
    vb.initialize(session_id=sid, hermes_home=str(tmp_path), provider_config=cfg)
    return vb


def _vstore(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "plugin-data" / "memrack" / "vec-index.db"))
    try:
        conn.enable_load_extension(True)
        import sqlite_vec as _sv
        _sv.load(conn)
        conn.enable_load_extension(False)
    except (ImportError, sqlite3.Error):
        pass
    return conn


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


# T-047: the harvest mirror is ENDED — the vec store carries its OWN corpus;
# there is no cross-store prune law left to pin (the old
# test_remove_prunes_facts_vec encoded the harvested law, retired with it).

# ── 3) vec hits carry the chunk id, dimension probe, embed batch ─────────────

def test_vec_doc_hits_carry_chunk_id(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    out = vb.add_source("wiki:sources/hit.md", text="Semantic recall probe sentence here.")
    hits = vb.search("Semantic recall probe sentence", "vec", 8)
    assert hits and hits[0]["kind"] == "doc"
    assert hits[0]["chunk_id"] >= 1 and hits[0]["source_ref"] == "wiki:sources/hit.md"
    assert out["chunks"] >= 1


def test_doc_hits_burn_vec_heat_not_ledger(tmp_path, monkeypatch):
    """Doc hits bump the vec backend's OWN per-chunk counter — the ledger is
    a base-db matter only (the T-047 heat flip)."""
    import sqlite3
    led = _engine(tmp_path)
    led.on_memory_write("add", "memory", "ledger heat probe entry stays untouched")
    content = "The doc heat probe sentence lives in the doc store."
    vb = _fake_vec(tmp_path, monkeypatch)
    vb.add_source("wiki:sources/heat.md", text=content)
    prov = mr._MemrackBase()
    prov.initialize(session_id="H2", hermes_home=str(tmp_path))
    prov._rack["vec"] = vb           # vec rides alongside the floor (not replaced)
    hits = vb.search("doc heat probe sentence", "vec", 8)
    assert hits and hits[0]["kind"] == "doc"
    prov._touch_facts(hits)
    st = _vstore(tmp_path)
    try:
        assert st.execute("SELECT heat FROM chunk_heat WHERE chunk_id = ?",
                          (hits[0]["chunk_id"],)).fetchone()[0] == 1, \
            "doc hits burn the OWN per-chunk counter"
    finally:
        st.close()
    fresh = sqlite3.connect(str(tmp_path / "plugin-data" / "memrack" / "memrack.db"))
    try:
        ledger_heat = fresh.execute(
            "SELECT hits FROM facts WHERE content LIKE 'ledger heat probe%'").fetchone()[0]
    finally:
        fresh.close()
    assert ledger_heat == 0, \
        "doc hits must NOT burn LEDGER heat (the kind gate: fact burns ledger, doc does not)"
    # a fact-kind hit keeps burning LEDGER heat as before (through its engine)
    prov._touch_facts([{"backend": "base-db", "kind": "fact", "id": 1}])
    led._db.commit()   # release the shared lock before the cross-connection read
    fresh = sqlite3.connect(str(tmp_path / "plugin-data" / "memrack" / "memrack.db"))
    try:
        bumped = fresh.execute(
            "SELECT hits FROM facts WHERE content LIKE 'ledger heat probe%'").fetchone()[0]
    finally:
        fresh.close()
    assert bumped == ledger_heat + 1, (ledger_heat, bumped)


def test_ingest_embed_batch_is_configurable(tmp_path, monkeypatch, ):
    vb = _fake_vec(tmp_path, monkeypatch, batch="1")
    out = vb.add_source("wiki:sources/b.md", text=_batch_doc(4, 60), chunk_chars=50)
    assert out["chunks"] >= 3, out
    st = _vstore(tmp_path)
    try:
        n = st.execute("SELECT COUNT(*) FROM facts_vec2").fetchone()[0]
    finally:
        st.close()
    assert n == out["chunks"], "every chunk is embedded (batch=1, no backlog: the harvest is ended)"


def _batch_doc(n, per=60):
    return " ".join("Satz %d mit Fülltext bis zur Grenze." % i for i in range(n))


def test_vec_dimension_probe_sizes_new_table_on_ingest(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch, dim=8)
    vb.add_source("wiki:sources/dim.md", text="Dimension probe content in here.")
    hits = vb.search("Dimension probe content", "vec", 8)
    assert hits
    assert vb._store.execute("SELECT vec_length(embedding) FROM facts_vec2 LIMIT 1"
                             ).fetchone()[0] == 8


def test_vec_dimension_mismatch_degrades_honestly(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch, dim=4)
    vb.add_source("wiki:sources/mm.md", text="first stored fact content here")
    vecspec = importlib.util.spec_from_file_location(
        "memrack_backends_vec3", pathlib.Path(_ROOT / "backends" / "vec.py"))
    VEC = importlib.util.module_from_spec(vecspec)
    vecspec.loader.exec_module(VEC)
    vb2 = VEC.VecBackend()
    monkeypatch.setattr(vb2, "_probe", lambda: True)
    monkeypatch.setattr(vb2, "_embed", lambda texts: [[0.1] * 6 for _ in texts])
    vb2.initialize(session_id="R3V2", hermes_home=str(tmp_path))
    assert vb2._vec_ok is False
    assert "dimension mismatch" in vb2._vec_gap
    assert vb2.card_modes() == ()
    with pytest.raises(ValueError):
        vb2.search("anything", "vec", 8)


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
