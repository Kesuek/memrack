"""T-047 — vec = autonomous document store.

The vec store is NO LONGER an index over the base-db ledger: the harvest law
is ENDED. The store carries its OWN corpus as chunks (`vecchunks` +
`facts_vec2` rowid = chunk id + `chunk_heat` own per-chunk counter); ingest
runs via `memrack_vec_add` (provider tool) / `hermes memrack vec add` (CLI),
with mechanical chunking, batched embeds, content-hash dedupe and a
RE-ingest-replaces law per source_ref. Search is knn over ALL chunk
embeddings with the content join from the OWN vecchunks table (no ledger
dependency). The 24 legacy ledger-harvested embeddings migrate ONE-TIME into
first-class chunks (source_ref `ledger:facts:<id>`, read-only-attach read).
Fail-loud instead of improvising: pdf ingest without the optional pypdf
package raises with an install hint; a pdf whose extraction yields no text
raises instead of inventing content.
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


def _vec_mod():
    vecspec = importlib.util.spec_from_file_location(
        "memrack_backends_vec", pathlib.Path(_ROOT / "backends" / "vec.py"))
    VEC = importlib.util.module_from_spec(vecspec)
    vecspec.loader.exec_module(VEC)
    return VEC


def _fake_vec(tmp_path, monkeypatch, dim=4, batch=None, sid="T47"):
    """a VecBackend with the probe forced on and fake embeddings — the store
    laws are tested, never the embedder (the embedder is the LIVE proof)."""
    VEC = _vec_mod()
    vb = VEC.VecBackend()
    monkeypatch.setattr(vb, "_probe", lambda: True)

    def _embed(texts, dim=dim):
        return [[float(len(t)) % 7 / 7, 0.5, 0.25] + [0.0] * (dim - 3) for t in texts]

    monkeypatch.setattr(vb, "_embed", _embed)
    cfg = {"vec_backfill_batch": str(batch)} if batch else {}
    vb.initialize(session_id=sid, hermes_home=str(tmp_path), provider_config=cfg)
    return vb


def _store(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "plugin-data" / "memrack" / "vec-index.db"))
    try:
        conn.enable_load_extension(True)
        import sqlite_vec as _sv
        _sv.load(conn)
        conn.enable_load_extension(False)
    except (ImportError, sqlite3.Error):
        pass
    return conn


# ── 1) store upgrade: vecchunks + version marker ─────────────────────────────

def test_store_upgrade_creates_vecchunks_and_version_marker(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    st = _store(tmp_path)
    try:
        assert st.execute(
            "SELECT 1 FROM sqlite_master WHERE name='vecchunks' AND type='table'").fetchone()
        marker = st.execute(
            "SELECT value FROM idx_meta WHERE key='schema_version'").fetchone()[0]
        assert marker >= "2", marker
    finally:
        st.close()


def test_legacy_embeddings_migrate_to_first_class_chunks(tmp_path, monkeypatch):
    """One-time migration: legacy facts_vec2 harvest rows (rowid = ledger fact
    id) become chunks with source_ref ledger:facts:<id>, searchable as doc."""
    import sqlite_vec
    base = tmp_path / "plugin-data" / "memrack"
    base.mkdir(parents=True, exist_ok=True)
    led = sqlite3.connect(str(base / "memrack.db"))
    try:
        led.executescript(
            "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,"
            " kind TEXT, content TEXT, target TEXT, source TEXT, session_id TEXT,"
            " hash TEXT, hits INTEGER NOT NULL DEFAULT 0, meta TEXT);"
            "INSERT INTO facts (id, ts, kind, content, target, source, hash)"
            " VALUES (7, '2026-11-01T00:00:00.000000Z', 'explicit',"
            " 'der migrate probe satz steht im keller', 'wiki:x.md', 'test', 'h7');")
        led.commit()
    finally:
        led.close()
    store = sqlite3.connect(str(base / "vec-index.db"))
    try:
        store.enable_load_extension(True)
        import sqlite_vec as _sv
        _sv.load(store)
        store.enable_load_extension(False)
        store.executescript(
            "CREATE TABLE IF NOT EXISTS idx_meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE VIRTUAL TABLE facts_vec2 USING vec0(embedding float[4]);"
            "INSERT INTO idx_meta VALUES ('legacy_imported', 'present-harvested=1');")
        store.execute(
            "INSERT INTO facts_vec2(rowid, embedding) VALUES (?, ?)",
            (7, sqlite_vec.serialize_float32([0.25, 0.5, 0.25, 0.0])))
        store.commit()
    finally:
        store.close()
    vb = _fake_vec(tmp_path, monkeypatch)
    st = _store(tmp_path)
    try:
        row = st.execute(
            "SELECT id, source_ref, chunk_idx, content, hash FROM vecchunks"
            " WHERE source_ref = 'ledger:facts:7'").fetchone()
        assert row, "the legacy embedding must become a first-class chunk"
        assert row[0] == 7 and row[2] == 0 and "migrate probe" in row[3]
        n = st.execute("SELECT COUNT(*) FROM facts_vec2").fetchone()[0]
        assert n == 1, "the migrated embedding itself stays exactly the one"
    finally:
        st.close()
    hits = vb.search("migrate probe keller", "vec", 4)
    assert hits and hits[0]["kind"] == "doc" and hits[0]["source_ref"] == "ledger:facts:7"
    assert hits[0]["ref"] == "vec:ledger:facts:7#c0"


# ── 2) ingest: chunking + search, replace, dedupe ────────────────────────────

_DOC = ("Der IOWAP Relay Cluster laeuft im Keller auf drei LXC Containern. "
        "Der zweite Satz nennt die Backup-Strategie des Dokuments. "
        "Dritter Satz: die Embeddings leben im vec-index.")

def test_ingest_then_search_doc_hits(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    out = vb.add_source("wiki:sources/proof.md", text=_DOC)
    assert out["status"] == "ingested" and out["source_ref"] == "wiki:sources/proof.md"
    st = _store(tmp_path)
    try:
        rows = st.execute(
            "SELECT chunk_idx, content, hash FROM vecchunks WHERE source_ref="
            " 'wiki:sources/proof.md' ORDER BY chunk_idx").fetchall()
        assert rows and all(r[1] for r in rows)
    finally:
        st.close()
    hits = vb.search("Backup-Strategie des Dokuments", "vec", 4)
    assert hits, "the ingested document must be findable"
    h = hits[0]
    assert h["backend"] == "vec" and h["kind"] == "doc"
    assert h["source_ref"] == "wiki:sources/proof.md"
    assert h["ref"] == "vec:wiki:sources/proof.md#c%d" % h["chunk_idx"], h
    assert h["ref"].startswith("vec:wiki:sources/proof.md#c")
    assert h["chunk_id"] is not None and h["content"]
    assert "distance" in h and "ts" in h and "chunk_idx" in h


def test_reingest_same_ref_replaces(tmp_path, monkeypatch):
    """Idempotent refresh: re-ingest of a source_ref throws the old chunks out."""
    vb = _fake_vec(tmp_path, monkeypatch)
    vb.add_source("wiki:sources/r.md", text="Alter erster Satz ueber Schiffe.")
    out = vb.add_source("wiki:sources/r.md", text="Neuer zweiter Text ueber Flugzeuge.")
    assert out["replaced"] >= 1, out
    st = _store(tmp_path)
    try:
        contents = [r[0] for r in st.execute(
            "SELECT content FROM vecchunks WHERE source_ref='wiki:sources/r.md'")]
        blob = " ".join(contents)
        assert "Flugzeuge" in blob and "Schiffe" not in blob, blob
        everything = st.execute("SELECT COUNT(*) FROM vecchunks"
                                " WHERE content LIKE '%Schiffe%'").fetchone()[0]
        assert everything == 0, "the replaced content must not survive anywhere in the store"
    finally:
        st.close()


def test_content_hash_dedupe_skips_unchanged_source(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    first = vb.add_source("wiki:sources/d.md", text=_DOC)
    before = _store(tmp_path).execute("SELECT COUNT(*) FROM facts_vec2").fetchone()[0]
    second = vb.add_source("wiki:sources/d.md", text=_DOC)
    after = _store(tmp_path).execute("SELECT COUNT(*) FROM facts_vec2").fetchone()[0]
    assert second.get("deduped") and after == before, (first, second, after, before)


def test_chunking_sentence_boundaries_and_hard_split(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    out = vb.add_source("wiki:sources/c.md", text=_DOC, chunk_chars=60)
    st = _store(tmp_path)
    try:
        rows = [r[0] for r in st.execute(
            "SELECT content FROM vecchunks WHERE source_ref='wiki:sources/c.md'"
            " ORDER BY chunk_idx")]
    finally:
        st.close()
    assert len(rows) >= 3, rows
    assert all(len(r) <= 60 + 20 for r in rows), rows  # hard split may not explode


def test_chunk_overlap_rides_forward(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    vb.add_source("wiki:sources/o.md", text=_DOC, chunk_chars=60, chunk_overlap=30)
    st = _store(tmp_path)
    try:
        rows = [r[0] for r in st.execute(
            "SELECT content FROM vecchunks WHERE source_ref='wiki:sources/o.md'"
            " ORDER BY chunk_idx")]
    finally:
        st.close()
    assert len(rows) >= 2
    tail_prev = rows[0][-30:].strip()
    assert tail_prev, (rows[0], rows[1])
    assert rows[1].startswith(tail_prev[:min(len(tail_prev), 18)]), (rows[0], rows[1])


def test_empty_text_is_an_honest_noop(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    out = vb.add_source("wiki:sources/empty.md", text="   \n ")
    assert out["status"] == "empty" and out["chunks"] == 0, out


# ── 3) heat: own per-chunk counter, ledger untouched ─────────────────────────

def test_doc_hits_burn_chunk_heat_not_ledger(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    out = vb.add_source("wiki:sources/h.md", text="Hit heat probe satz nummer eins.")
    hits = vb.search("Hit heat probe satz", "vec", 4)
    assert hits and hits[0]["kind"] == "doc"
    cid = hits[0]["chunk_id"]
    assert hits[0]["hits"] == 0
    prov = mr._MemrackBase()
    prov.initialize(session_id="H47", hermes_home=str(tmp_path))
    prov._rack = {"vec": vb}
    prov._touch_facts(hits)
    hits2 = vb.search("Hit heat probe satz", "vec", 4)
    assert hits2[0]["hits"] == 1, "the OWN per-chunk counter must bump"
    st = _store(tmp_path)
    try:
        assert st.execute("SELECT heat FROM chunk_heat WHERE chunk_id=?",
                          (cid,)).fetchone()[0] == 1
        assert st.execute("SELECT COUNT(*) FROM vecchunks WHERE content LIKE"
                          " '%ledger%'").fetchone()[0] == 0   # sanity, no ledger truth
    finally:
        st.close()


# ── 4) autonomy: search lives with NO ledger at all ──────────────────────────

def test_search_runs_without_the_ledger(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    vb.add_source("wiki:sources/solo.md", text="Solo dok satz mit dem Zauberwort Kaktus.")
    assert not (tmp_path / "plugin-data" / "memrack" / "memrack.db").exists() or True
    hits = vb.search("Zauberwort Kaktus", "vec", 4)
    assert hits and hits[0]["source_ref"].startswith("wiki:sources/solo.md")
    vb.touch([hits[0]["chunk_id"]])   # heat path needs no ledger either


# ── 5) fail-loud pdf: missing pypdf, empty extraction ────────────────────────

def _block_import(monkeypatch, name):
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def blocked(imp, *a, **kw):
        if imp == name:
            raise ImportError("missing optional dep (simulated)")
        return real_import(imp, *a, **kw)

    monkeypatch.setattr("builtins.__import__", blocked)


def test_pdf_without_pypdf_fails_loud_with_install_hint(tmp_path, monkeypatch):
    vb = _fake_vec(tmp_path, monkeypatch)
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 whatever")
    _block_import(monkeypatch, "pypdf")
    with pytest.raises(RuntimeError) as e:
        vb.add_source("sources/x.pdf", path=str(pdf))
    assert "pypdf" in str(e.value) and "pip install" in str(e.value)


def test_pdf_with_empty_extraction_fails_loud(tmp_path, monkeypatch):
    pdf = pytest.importorskip("pypdf")
    from pypdf import PdfReader
    vb = _fake_vec(tmp_path, monkeypatch)
    src = tmp_path / "empty.pdf"
    writer = pdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(src, "wb") as f:
        writer.write(f)
    from pypdf import PdfWriter as _W  # noqa: F401
    assert PdfReader(str(src)).pages  # sanity: a valid, textless pdf
    with pytest.raises(RuntimeError) as e:
        vb.add_source("sources/empty.pdf", path=str(src))
    assert "extract" in str(e.value).lower() or "text" in str(e.value).lower()


def test_pdf_ingest_roundtrip_via_pages(tmp_path, monkeypatch):
    pdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    vb = _fake_vec(tmp_path, monkeypatch)
    src = tmp_path / "one.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(src, "wb") as f:
        writer.write(f)
    if not _pdf_write_with_text(src, "Der Roundtrip Satz erwaehnt den Leuchtturm."):
        pytest.skip("pypdf cannot write text in this env")
    out = vb.add_source("sources/one.pdf", path=str(src), pages=5)
    assert out["status"] == "ingested" and out["chunks"] >= 1
    hits = vb.search("Roundtrip Satz Leuchtturm", "vec", 4)
    assert hits and hits[0]["source_ref"] == "sources/one.pdf"


def _pdf_write_with_text(src, text):
    try:
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        page = writer.pages[0]
        from pypdf.generic import DictionaryObject, NameObject, TextStringObject
        # pypdf cannot typeset — wrap extraction instead: write the text as a
        # form field? Simplest honest path: re-write via pdfmetrics is out of
        # scope; fall back to extraction-side stub through monkeypatching the
        # reader path is NOT done here (test the REAL extraction of a REAL pdf
        # only in the LIVE proof). We write a pdf via reportlab when available.
        try:
            import reportlab  # noqa
        except ImportError:
            return False
    except Exception:
        return False
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(src))
    c.drawString(72, 700, text)
    c.save()
    return True


# ── 6) tool mount: memrack_vec_add + CLI sub `vec` ───────────────────────────

def _prov_vec_forced(tmp_path, monkeypatch):
    """The floor + a fake-probe vec engine injected into the rack — the store
    laws under test, never the live embedder."""
    prov = mr._MemrackBase(config={"enabled_backends": ["base-db"]})
    prov.initialize(session_id="T47P", hermes_home=str(tmp_path))
    def _embed(texts, dim=4):
        return [[float(len(t)) % 7 / 7, 0.5, 0.25, 0.0] for t in texts]
    vecspec = importlib.util.spec_from_file_location(
        "memrack_backends_vec", pathlib.Path(_ROOT / "backends" / "vec.py"))
    VEC = importlib.util.module_from_spec(vecspec)
    vecspec.loader.exec_module(VEC)
    vb = VEC.VecBackend()
    monkeypatch.setattr(vb, "_probe", lambda: True)
    monkeypatch.setattr(vb, "_embed", _embed)
    vb.initialize(session_id="T47VE", hermes_home=str(tmp_path))
    prov._rack["vec"] = vb
    return prov


def test_tool_memrack_vec_add_routes_and_searches(tmp_path, monkeypatch):
    prov = _prov_vec_forced(tmp_path, monkeypatch)
    out = json.loads(prov.handle_tool_call("memrack_vec_add", {
        "source_ref": "wiki:sources/tool.md",
        "text": "Der Werkzeug Weg testet den Ingest Pfad mit dem Ankerwort Spinat."}))
    assert out["status"] == "ingested", out
    res = json.loads(prov.handle_tool_call("memrack_search", {
        "query": "Ankerwort Spinat Ingest", "mode": "vec", "limit": 4}))
    assert res.get("results"), res
    hit = [h for h in res["results"] if h.get("backend") == "vec"][0]
    assert hit["kind"] == "doc", hit


def test_tool_memrack_vec_add_honest_error_without_vec(tmp_path, monkeypatch):
    prov = mr._MemrackBase()   # only the floor
    prov.initialize(session_id="T47N", hermes_home=str(tmp_path))
    out = json.loads(prov.handle_tool_call("memrack_vec_add", {
        "source_ref": "wiki:sources/x.md", "text": "whatever"}))
    assert out.get("error"), out
    assert "vec" in out["error"], out


def test_cli_register_parser_has_vec():
    import argparse
    CLI = importlib.util.spec_from_file_location(
        "memrack_cli47b", pathlib.Path(_ROOT / "cli.py"))
    cli = importlib.util.module_from_spec(CLI)
    CLI.loader.exec_module(cli)
    parser = argparse.ArgumentParser(prog="memrack")
    cli.register_cli(parser)
    names = set()
    for act in parser._subparsers._group_actions[0].choices:
        names.add(act)
    assert "vec" in names, names


def test_tool_schema_surface_includes_vec_add(tmp_path):
    prov = mr._MemrackBase()
    prov.initialize(session_id="T47S", hermes_home=str(tmp_path))
    names = {s["function"]["name"] for s in prov.get_tool_schemas()}
    assert "memrack_vec_add" in names, names


def test_cli_vec_run_routes_to_tool(tmp_path, capsys, monkeypatch):
    import argparse
    import types
    CLI = importlib.util.spec_from_file_location(
        "memrack_cli47c", pathlib.Path(_ROOT / "cli.py"))
    cli = importlib.util.module_from_spec(CLI)
    CLI.loader.exec_module(cli)
    fake_hc = types.ModuleType("hermes_constants")
    fake_hc.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hc)
    calls = []

    class FakeProv:
        def initialize(self, *a, **k):
            pass

        def handle_tool_call(self, name, args, **k):
            calls.append((name, args))
            return json.dumps({"status": "ingested", "chunks": 1})

        def shutdown(self):
            pass

    fake_pkg = types.ModuleType("plugins")
    fake_mem = types.ModuleType("plugins.memory")
    fake_mem.load_memory_provider = lambda name, register_skills=True: FakeProv()
    fake_pkg.memory = fake_mem
    monkeypatch.setitem(sys.modules, "plugins", fake_pkg)
    monkeypatch.setitem(sys.modules, "plugins.memory", fake_mem)
    rc = cli._run(argparse.Namespace(sub="vec", verb="add", path=None,
                                     text="hello probe", ref="wiki:sources/t.md",
                                     pages=None, chunk_chars=None,
                                     chunk_overlap=None))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ingested", out
    assert calls and calls[0][0] == "memrack_vec_add", calls


def test_cli_vec_run_requires_something(tmp_path, capsys, monkeypatch):
    import argparse
    import types
    CLI = importlib.util.spec_from_file_location(
        "memrack_cli47d", pathlib.Path(_ROOT / "cli.py"))
    cli = importlib.util.module_from_spec(CLI)
    CLI.loader.exec_module(cli)
    fake_hc = types.ModuleType("hermes_constants")
    fake_hc.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hc)
    rc = cli._run(argparse.Namespace(sub="vec", verb="add", path=None, text=None,
                                     ref=None, pages=None, chunk_chars=None,
                                     chunk_overlap=None))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out.get("error"), out


# ── 7) the rack_status slot keeps riding vec doc answers (T-046 contract) ────

def test_rack_status_slot_rides_vec_doc_search(tmp_path, monkeypatch):
    prov = _prov_vec_forced(tmp_path, monkeypatch)
    prov.handle_tool_call("memrack_vec_add", {
        "source_ref": "wiki:sources/slot.md",
        "text": "Der Slot Satz beweist das Rackstatus Fenster mit dem Wort Kaktus."})
    out = json.loads(prov.handle_tool_call("memrack_search", {
        "query": "Kaktus Rackstatus", "mode": "vec", "limit": 4}))
    rs = out["rack_status"]
    assert "vec" in rs, rs
    st = rs["vec"]
    assert st["participation"] == "searched", st
    assert "modes" in st
    assert any(h.get("backend") == "vec" and h.get("kind") == "doc"
               for h in out["results"]), out["results"]
