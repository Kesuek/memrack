"""Built-in "vec" backend: an AUTONOMOUS DOCUMENT STORE (opt-in).

Autonomy law (T-047, 2026-11-09): the vec store carries its OWN corpus as
chunks — it is no longer an index over the base-db ledger. The automatic
ledger harvest is ENDED; the fact ledger plays no role in search anymore
(no attach in the read path, no auto-embedding of base-db facts).

Store schema (``plugin-data/memrack/vec-index.db``):
  - ``vecchunks``  (id PK, source_ref, chunk_idx, content, hash, ts)
                   UNIQUE(source_ref, chunk_idx)
  - ``facts_vec2`` (vec0 embeddings; rowid = chunk id)
  - ``chunk_heat`` (chunk_id PK, heat) — the OWN per-chunk heat counter
  - ``idx_meta``   model/dimension/schema_version/legacy_imported/src_hash:*
One-time migration: legacy facts_vec2 harvest rows (rowid = ledger fact id)
become first-class chunks with ``source_ref = ledger:facts:<id>`` via a
READ-ONLY-attach read of the ledger — the canonical ledger is never mutated.

Ingest: ``add_source`` (the ``memrack_vec_add`` tool / ``hermes memrack vec
add`` CLI): text or path → mechanical chunks (sentence boundaries when
possible, otherwise a hard split) → batched embeds → upsert. Re-ingest of a
source_ref REPLACES (old chunks of the ref out, new in). No LLM calls; the
embeddings are the only model contact. PDFs need the OPTIONAL dep pypdf —
fail loud with an install hint when it is missing, and fail loud when a pdf
extraction yields no text.

Membership: off by default (``vec_in_rack`` toggle); when disabled the rack
has zero Ollama contact and zero cold start.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

_LOG = logging.getLogger("memrack.backends.vec")

PLUGIN_NAME = "vec"

_EMBED_MODEL = "nomic-embed-text"
_OLLAMA_HOST = "http://127.0.0.1:11434"

_SCHEMA_VERSION = "2"

_SENTENCE = re.compile(r"[^.!?\n]*[.!?…]+[\"')\]]?\s*")

_PDF_HINT = ("pdf ingest needs the optional dep pypdf — install it with "
             "'pip install pypdf' (declared in plugin.yaml pip_dependencies)")


def _utcnow_iso():
    """UTC-Z with microseconds — the cross-store chronology law."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "%06dZ" % (time.time() % 1 * 1e6)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def chunk_text(text, chunk_chars=1200, chunk_overlap=0):
    """Mechanical chunking: sentence boundaries when possible, a hard split
    when a single sentence exceeds the window — NO LLM calls."""
    size = max(50, min(int(chunk_chars), 8000))
    ov = max(0, min(int(chunk_overlap), size // 2))
    chunks = []
    buf = ""
    raw = text
    sentences = []
    pos = 0
    while pos < len(raw):
        m = _SENTENCE.match(raw, pos)
        if m and m.end() < len(raw):
            sentences.append(raw[pos:m.end()])
            pos = m.end()
        else:
            sentences.append(raw[pos:])
            pos = len(raw)
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        while len(s) > size:   # hard split of an over-long sentence
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(s[:size])
            s = s[size:]
        cand = (buf + " " + s).strip() if buf else s
        if len(cand) <= size:
            buf = cand
        else:
            if buf:
                chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    if ov and len(chunks) > 1:
        carried = []
        for i, c in enumerate(chunks):
            if i == 0:
                carried.append(c)
            else:
                tail = chunks[i - 1][-ov:]
                carried.append((tail + " " + c).strip())
        chunks = carried
    return chunks


class VecBackend:
    """Autonomous document store over a local embedding model (sqlite-vec)."""

    name = PLUGIN_NAME
    best_for = ("autonomous document corpus: ingest .md/.txt/pdf into chunks and "
                "recall them semantically via a local embedding model (sqlite-vec); "
                "opt-in — the floor stays table-only for ms-fast fts")
    limits = ("an INDEPENDENT corpus — ONLY what was ingested here is in (no automatic "
              "ledger harvest); pdf ingest needs the optional pypdf dep; requires a "
              "reachable local embedder AND its membership toggle; cold start on the "
              "first query after a restart lives here")
    search_modes = ("vec",)
    writeable = False   # no atomic facts: ingest goes through add_source/vec add

    def __init__(self):
        self._store: sqlite3.Connection | None = None
        self._store_path: Path | None = None
        self._lock = threading.Lock()
        self._provider_config: dict = {}
        self._model = _EMBED_MODEL
        self._host = _OLLAMA_HOST
        self._vec_ok = False
        self._vec_gap = "not initialized"
        self._batch = 32
        self._ledger_path: Path | None = None

    # ── lifecycle ──
    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        self._provider_config = dict(provider_config or {})
        self._model = self._provider_config.get("embedding_model") or self._model
        self._host = self._provider_config.get("ollama_host") or self._host
        try:
            self._batch = max(1, int(self._provider_config.get("vec_backfill_batch") or 32))
        except (TypeError, ValueError):
            self._batch = 32
        base = Path(hermes_home or (Path.home() / ".hermes")) / "plugin-data" / "memrack"
        base.mkdir(parents=True, exist_ok=True)
        import urllib.parse
        self._ledger_path = base / "memrack.db"
        self._store_path = base / "vec-index.db"
        self._store = sqlite3.connect(
            "file:" + urllib.parse.quote(str(self._store_path), safe="/") + "?mode=rwc",
            check_same_thread=False, timeout=5.0, uri=True)
        self._vec_ok = False
        self._vec_gap = "embedder not probed yet"
        self._upgrade_store()   # vecchunks/chunk_heat/version — embedder-independent
        if not self._probe():
            self._log_state()
            return  # honest degrade: store stays open, modes stay empty until the embedder shows up
        try:
            import sqlite_vec
            self._store.enable_load_extension(True)
            sqlite_vec.load(self._store)
            self._store.enable_load_extension(False)
            dim = self._dimension()
            self._adopt_dim(dim)
            self._store.commit()
            self._vec_ok = bool(dim) and "mismatch" not in self._vec_gap
            if self._vec_ok:
                self._migrate_legacy()
        except (sqlite3.OperationalError, AttributeError) as exc:
            self._vec_ok = False
            self._vec_gap = "extension/store setup failed: %s" % exc
            _LOG.warning("vec backend degraded: %s", exc)
        self._log_state()

    def _log_state(self):
        if self._vec_ok:
            _LOG.info("vec backend ready (%s, store %s)", self._model, self._store_path)
        else:
            _LOG.warning("vec backend unavailable: %s", self._vec_gap)

    # ── store upgrade (T-047): vecchunks + chunk_heat + version marker ──
    def _upgrade_store(self):
        with self._lock:
            self._store.executescript(
                "CREATE TABLE IF NOT EXISTS vecchunks ("
                "  id INTEGER PRIMARY KEY,"
                "  source_ref TEXT NOT NULL,"
                "  chunk_idx INTEGER NOT NULL,"
                "  content TEXT NOT NULL,"
                "  hash TEXT NOT NULL,"
                "  ts TEXT NOT NULL,"
                "  UNIQUE(source_ref, chunk_idx));"
                "CREATE TABLE IF NOT EXISTS chunk_heat ("
                "  chunk_id INTEGER PRIMARY KEY,"
                "  heat INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS idx_meta ("
                "  key TEXT PRIMARY KEY, value TEXT);")
            row = self._store.execute(
                "SELECT value FROM idx_meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._store.execute(
                    "INSERT INTO idx_meta(key, value) VALUES('schema_version', ?)",
                    (_SCHEMA_VERSION,))
                self._store.execute(
                    "INSERT INTO idx_meta(key, value) VALUES('version_law', ?)",
                    ("t047-vec-autonomous",))
            self._store.commit()

    # ── embedder probe / embedding ──
    def _probe(self):
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            self._vec_gap = "sqlite-vec package not installed (optional dep)"
            return False
        try:
            import urllib.request
            with urllib.request.urlopen(self._host.rstrip("/") + "/api/tags", timeout=1) as resp:
                tags = json.load(resp)
        except Exception as exc:
            self._vec_gap = "%s not reachable: %s" % (self._host, exc)
            return False
        names = [str(m.get("name", "")).split(":")[0] for m in tags.get("models", [])]
        if self._model not in names:
            self._vec_gap = "embedding model %r not pulled (ollama list)" % self._model
            return False
        self._vec_gap = ""
        return True

    def _embed(self, texts, timeout=300):
        import urllib.request
        body = json.dumps({"model": self._model, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(self._host.rstrip("/") + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        return list(data.get("embeddings") or [])

    def _dimension(self):
        try:
            vecs = self._embed(["memrack dimension probe"])
        except Exception as exc:
            self._vec_gap = "dimension probe failed: %s: %s" % (type(exc).__name__, exc)
            return None
        if not vecs or not vecs[0]:
            self._vec_gap = "dimension probe returned no embedding"
            return None
        return len(vecs[0])

    def _adopt_dim(self, dim):
        assert self._store is not None
        if dim is None:
            self._vec_ok = False
            return
        exists = self._store.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts_vec2'").fetchone()
        if exists is None:
            self._store.executescript(
                "CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec2 USING vec0(embedding float[%d]);"
                % dim)
            return
        row = self._store.execute("SELECT vec_length(embedding) FROM facts_vec2 LIMIT 1").fetchone()
        if row and row[0] is not None and int(row[0]) != dim:
            self._vec_ok = False
            self._vec_gap = ("vec dimension mismatch: index expects %s, model %r produces %d"
                             % (row[0], self._model, dim))
            _LOG.warning("vec backend degraded: %s", self._vec_gap)

    # ── legacy migration: the ONE read-only-attach this backend still does ──
    def _migrate_legacy(self):
        """Convert legacy facts_vec2 harvest rows (rowid = ledger fact id) into
        first-class chunks; rows whose ledger fact is gone are dropped (honest
        count). Runs ONCE per store; the harvest itself is ENDED."""
        assert self._store is not None
        done = self._store.execute(
            "SELECT value FROM idx_meta WHERE key='chunks_migrated'").fetchone()
        if done:
            return
        try:
            rows = self._store.execute(
                "SELECT v.rowid FROM facts_vec2 v"
                " LEFT JOIN vecchunks c ON c.id = v.rowid WHERE c.id IS NULL").fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            with self._lock:
                self._store.execute(
                    "INSERT INTO idx_meta(key, value) VALUES('chunks_migrated', 'none-needed')")
                self._store.commit()
            return
        migrated = dropped = 0
        for (rid,) in rows:
            with self._lock:
                exists = self._store.execute(
                    "SELECT 1 FROM vecchunks WHERE id = ?", (rid,)).fetchone()
            if exists:
                continue
            content = target = ts = None
            attached = self._ledger()
            try:
                if attached is not None:
                    row = self._store.execute(
                        "SELECT content, target, ts FROM ledger.facts f"
                        " WHERE f.id = ?", (rid,)).fetchone()
                    if row:
                        content, target, ts = row
            finally:
                self._detach()
            if content is None:
                with self._lock:
                    self._store.execute("DELETE FROM facts_vec2 WHERE rowid = ?", (rid,))
                    self._store.commit()
                dropped += 1
                continue
            if ts is None:
                ts = _utcnow_iso()
            source_ref = "ledger:facts:%d" % rid
            with self._lock:
                self._store.execute(
                    "INSERT OR REPLACE INTO vecchunks(id, source_ref, chunk_idx, content,"
                    " hash, ts) VALUES(?, ?, 0, ?, ?, ?)",
                    (rid, source_ref, content, _sha256(content), ts))
                self._store.commit()
            migrated += 1
        with self._lock:
            self._store.execute(
                "INSERT INTO idx_meta(key, value) VALUES('chunks_migrated', ?)",
                ("migrated=%d,dropped=%d" % (migrated, dropped),))
            self._store.commit()
        _LOG.info("vec backend: legacy harvest rows converted to chunks (%d migrated, %d dropped)",
                  migrated, dropped)

    def _ledger(self):
        assert self._store is not None
        if not self._ledger_path or not self._ledger_path.exists():
            return None
        self._store.execute("ATTACH DATABASE 'file:%s?mode=ro' AS ledger" % self._ledger_path)
        return True

    def _detach(self):
        try:
            self._store.execute("DETACH DATABASE ledger")
        except sqlite3.OperationalError:
            pass

    # ── ingest: the autonomous path (replace-by-ref, hash dedupe) ──
    def add_source(self, source_ref, text=None, path=None, chunk_chars=1200,
                   chunk_overlap=0, pages=None):
        """Ingest one source: read/extract → chunk → embed-batched → upsert.
        RE-ingest of a source_ref REPLACES (old chunks of the ref out). No LLM
        calls; the embeddings are the only model contact. Fail loud."""
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("source_ref required (a stable reading reference, "
                             "e.g. a wiki path or a file path)")
        source_ref = source_ref.strip()
        if (text is None) == (path is None):
            raise ValueError("exactly ONE of text/path is required")
        if not self._vec_ok:
            raise RuntimeError("vec store not ready: %s" % self._vec_gap)
        if path is not None:
            text = self._read_source(path, pages)
        assert self._store is not None
        import sqlite_vec
        text = str(text or "")
        if not text.strip():
            return {"status": "empty", "source_ref": source_ref, "chunks": 0}
        full_hash = _sha256(text)
        prior = self._store.execute(
            "SELECT value FROM idx_meta WHERE key = ?",
            ("src_hash:" + source_ref,)).fetchone()
        have = self._store.execute(
            "SELECT COUNT(*) FROM vecchunks WHERE source_ref = ?", (source_ref,)).fetchone()[0]
        if prior and prior[0] == full_hash and have:
            return {"status": "deduped", "deduped": True, "source_ref": source_ref,
                    "chunks": have}
        chunks = chunk_text(text, chunk_chars=chunk_chars or 1200,
                            chunk_overlap=chunk_overlap or 0)
        if not chunks:
            return {"status": "empty", "source_ref": source_ref, "chunks": 0}
        vecs = []
        for start in range(0, len(chunks), self._batch):
            window = chunks[start:start + self._batch]
            got = self._embed(window)
            if len(got) != len(window):
                raise RuntimeError("embedding count mismatch: %d != %d"
                                   % (len(got), len(window)))
            vecs.extend(got)
        ts = _utcnow_iso()
        old_ids = [r[0] for r in self._store.execute(
            "SELECT id FROM vecchunks WHERE source_ref = ?", (source_ref,)).fetchall()]
        with self._lock:
            next_id = self._store.execute(
                "SELECT COALESCE(MAX(id), 0) FROM vecchunks").fetchone()[0] + 1
            self._store.execute("DELETE FROM vecchunks WHERE source_ref = ?", (source_ref,))
            if old_ids:
                self._store.executemany("DELETE FROM chunk_heat WHERE chunk_id = ?",
                                        [(i,) for i in old_ids])
                self._store.execute(
                    "DELETE FROM facts_vec2 WHERE rowid NOT IN (SELECT id FROM vecchunks)")
            for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
                cid = next_id + i
                self._store.execute(
                    "INSERT INTO vecchunks(id, source_ref, chunk_idx, content, hash, ts)"
                    " VALUES(?, ?, ?, ?, ?, ?)",
                    (cid, source_ref, i, chunk, _sha256(chunk), ts))
                self._store.execute(
                    "INSERT OR REPLACE INTO facts_vec2(rowid, embedding) VALUES(?, ?)",
                    (cid, sqlite_vec.serialize_float32(vec)))
            self._store.execute(
                "INSERT OR REPLACE INTO idx_meta(key, value) VALUES(?, ?)",
                ("src_hash:" + source_ref, full_hash))
            self._store.commit()
        return {"status": "ingested", "source_ref": source_ref,
                "chunks": len(chunks), "replaced": len(old_ids)}

    # ── ingest path containment (Security-Review #154152) ──
    # A prompt-injected tool call must not ingest secrets ($HERMES_HOME/.env,
    # auth.json, ~/.ssh) into the corpus and ship them to the embedding host.
    # Rules: the path must resolve (symlinks resolved) under a CONFIGURED root
    # (vec_ingest_roots, fallback: the home dir; wiki_root rides along when
    # configured) — and the SECRET WALLS (HERMES_HOME, ~/.ssh, .env* names)
    # refuse even when a configured root happens to cover them. Walls win.
    def _ingest_roots(self):
        roots: "list[Path]" = []
        raw_conf = self._provider_config.get("vec_ingest_roots")
        if raw_conf:
            if isinstance(raw_conf, str):
                raw_conf = [x for x in raw_conf.split(",") if x.strip()]
            for raw in raw_conf:
                roots.append(Path(str(raw)).expanduser().resolve())
        wiki_root = self._provider_config.get("wiki_root")
        if wiki_root:
            roots.append(Path(str(wiki_root)).expanduser().resolve())
        return roots or [Path.home().resolve()]

    def _check_ingest_path(self, path):
        p = Path(str(path)).expanduser().resolve()
        hermes_home = Path(
            os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).resolve()
        ssh_dir = (Path.home() / ".ssh").resolve()
        if p == hermes_home or hermes_home in p.parents:
            raise PermissionError(
                "vec ingest path refused: inside HERMES_HOME — %s" % p)
        if p == ssh_dir or ssh_dir in p.parents:
            raise PermissionError(
                "vec ingest path refused: inside ~/.ssh — %s" % p)
        if any(str(part).startswith(".env") for part in [p.name, *p.parents]):
            raise PermissionError(
                "vec ingest path refused: .env file — %s" % p)
        roots = self._ingest_roots()
        if not any(p == r or r in p.parents for r in roots):
            raise PermissionError(
                "vec ingest path refused: outside the configured ingest roots "
                "(set vec_ingest_roots in the memrack config to widen) — %s "
                "(allowed roots: %s)" % (p, ", ".join(str(r) for r in roots)))
        return p

    def _read_source(self, path, pages=None):
        """Containment check → .md/.txt straight to text; .pdf via pypdf
        extraction (OPTIONAL dep, fail loud with an install hint; fail loud on
        empty extraction). The containment check runs FIRST — a prompt-injected
        path never reaches the reader, no matter how valid the file looks."""
        self._check_ingest_path(path)
        p = Path(str(path))
        if not p.exists():
            raise FileNotFoundError("source path not found: %s" % p)
        suffix = p.suffix.lower()
        if suffix == ".pdf":
            try:
                import pypdf  # noqa: F401
            except ImportError:
                raise RuntimeError(_PDF_HINT)
            from pypdf import PdfReader
            reader = PdfReader(str(p))
            page_list = reader.pages
            if pages is not None:
                page_list = page_list[:max(0, int(pages))]
            parts = []
            for page in page_list:
                try:
                    parts.append(page.extract_text() or "")
                except Exception as exc:   # a single broken page must not sink the doc
                    _LOG.warning("pdf page extraction failed: %s", exc)
            text = "\n\n".join(parts).strip()
            if not text.strip():
                raise RuntimeError(
                    "pdf extraction yielded no text (%s, %d page(s) read) — "
                    "no text layer (would need OCR, which this backend never runs)"
                    % (p.name, len(page_list)))
            return text
        return p.read_text(encoding="utf-8", errors="replace")

    # ── search: knn over ALL chunk embeddings; the join comes from OWN vecchunks ──
    def card_modes(self):
        return ("vec",) if self._vec_ok else ()

    def search(self, query, mode, k):
        if not self._vec_ok:
            raise ValueError("vec not available: %s" % self._vec_gap)
        if mode != "vec":
            raise ValueError("this engine carries mode %r not (modes: %s)" % (mode, ["vec"]))
        import sqlite_vec
        assert self._store is not None
        q_vecs = self._embed([query])
        if not q_vecs:
            return []
        qvec = sqlite_vec.serialize_float32(q_vecs[0])
        rows = self._store.execute(
            "SELECT v.rowid, v.distance, c.chunk_idx, c.source_ref, c.content, c.ts,"
            " COALESCE(h.heat, 0)"
            " FROM facts_vec2 v JOIN vecchunks c ON c.id = v.rowid"
            " LEFT JOIN chunk_heat h ON h.chunk_id = v.rowid"
            " WHERE v.embedding MATCH ? AND k = ?", (qvec, k)).fetchall()
        hits = []
        for i, (cid, dist, cidx, ref, content, ts, heat) in enumerate(rows, 1):
            if content is None:
                continue
            ref_read = "vec:%s#c%d" % (ref, cidx)
            hits.append({"backend": self.name, "kind": "doc", "id": cid,
                         "chunk_id": cid, "chunk_idx": cidx, "source_ref": ref,
                         "content": content, "ref": ref_read, "target": ref_read,
                         "rank": i, "ts": ts, "hits": int(heat),
                         "distance": round(float(dist), 4)})
        return hits

    # ── heat: the OWN per-chunk counter (the ledger plays no role) ──
    def touch(self, ids):
        clean = []
        for tid in ids:
            try:
                clean.append(int(tid))
            except (TypeError, ValueError):
                continue
        if not clean or self._store is None:
            return
        with self._lock:
            for _attempt in range(5):
                try:
                    self._store.executemany(
                        "INSERT INTO chunk_heat(chunk_id, heat) VALUES(?, 1)"
                        " ON CONFLICT(chunk_id) DO UPDATE SET heat = heat + 1",
                        [(i,) for i in clean])
                    self._store.commit()
                    break
                except sqlite3.OperationalError:
                    _LOG.warning("vec touch locked (attempt %d)", _attempt + 1)
                    time.sleep(0.2 * (_attempt + 1))

    # ── inert docked-contract stubs ──
    def on_turn(self, *a, **k):
        return None

    def on_memory_write(self, *a, **k):
        return {"status": "ignored"}

    def on_session_end(self, *a, **k):
        return None

    def prefetch(self, *a, **k):
        return ""

    def system_prompt_block(self):
        return ""

    def shutdown(self):
        pass

    @property
    def db_path(self):
        return self._store_path

    def export(self):
        chunks = 0
        docs = 0
        if self._store is not None:
            try:
                chunks = self._store.execute("SELECT COUNT(*) FROM vecchunks").fetchone()[0]
                docs = self._store.execute(
                    "SELECT COUNT(DISTINCT source_ref) FROM vecchunks").fetchone()[0]
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                pass
        return {"name": self.name, "best_for": self.best_for, "limits": self.limits,
                "search": list(self.card_modes()), "write": self.writeable,
                "built_in": True, "vec": self._vec_ok, "model": self._model,
                "store": str(self._store_path) if self._store_path else None,
                "chunks": chunks, "documents": docs,
                "vec_gap": None if self._vec_ok else self._vec_gap}


def export_backend():
    return VecBackend()
