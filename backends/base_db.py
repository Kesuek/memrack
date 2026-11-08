"""Built-in "base-db" backend — the stdlib floor of the rack.

SQLite-only store (facts + pointers + FTS5), zero external dependencies:
no vector engine, no network, no wiki requirement. Works solo — one backend
is the whole rack (Basisfall, the degradation law). The schema is intentional as-is;
see docs/schema-base-db.md.

Card (code-first built-in; docked backends carry theirs in the manifest):
    name:    base-db
    best_for: atomic facts + mirror of memory-writes; exact recall, milliseconds
limits:  ONE sentence per row, no prose
    search:  fts, pointers
"""

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

_LOG = logging.getLogger("memrack.base-db")


def _utcnow_iso():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

PLUGIN_NAME = "base-db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,   -- fresh stores: a deleted id is never recycled
  ts      TEXT NOT NULL,
  kind    TEXT NOT NULL,
  content TEXT NOT NULL,
  target  TEXT,
  source  TEXT NOT NULL,
  session_id TEXT,
  hash    TEXT NOT NULL,
  hits    INTEGER NOT NULL DEFAULT 0,
  meta    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS fa_hash ON facts(hash);
CREATE TABLE IF NOT EXISTS pointers (
  id       INTEGER PRIMARY KEY,
  subject  TEXT NOT NULL,
  location TEXT NOT NULL,
  kind     TEXT,
  ts       TEXT NOT NULL,
  checked  TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    content, content='facts', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS pointers_fts USING fts5(
    subject, location, content='pointers', content_rowid='id');
"""


# The vec table is created DYNAMICALLY at initialize with the dimension probed from the
# configured embedding model (facts_vec: rowid = the fact id it indexes).


class BaseDbBackend:
    name = PLUGIN_NAME
    best_for = "atomic facts + mirror of memory-writes; exact recall in milliseconds"
    limits = "ONE sentence per row on write; import allows multi-sentence legacy rows (tags import:)"
    search_modes = ("fts", "pointers")   # STATIC floor; card_modes() reports the DYNAMIC set
    writeable = True
    # the write-kind vocabulary — legacy input "curation" is tolerated and normalized to "curated"
    write_types = ("explicit", "curated")

    _EMBED_MODEL = "nomic-embed-text"
    _OLLAMA_HOST = "http://127.0.0.1:11434"

    def __init__(self):
        self._db: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._lock = threading.Lock()
        self._provider_config: dict = {}
        self._vec_ok = False
        self._vec_gap = "not probed yet"
        self._vec_batch = 32

    # ── lifecycle ──
    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        self._provider_config = dict(provider_config or {})
        self._EMBED_MODEL = self._provider_config.get("embedding_model") or self._EMBED_MODEL
        self._OLLAMA_HOST = self._provider_config.get("ollama_host") or self._OLLAMA_HOST
        self._vec_batch = self._read_vec_batch()
        base = Path(hermes_home or (Path.home() / ".hermes")) / "plugin-data" / "memrack"
        base.mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.chmod(base, 0o700)
        except OSError:
            pass
        self._path = base / "memrack.db"
        self._db = sqlite3.connect(str(self._path), check_same_thread=False, timeout=5.0)
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=3000")
            self._db.commit()
        # vec capability probe: optional upgrade — degrade to fts-only with a visible gap
        self._vec_ok = self._vec_probe()
        if self._vec_ok:
            try:
                import sqlite_vec
                self._db.enable_load_extension(True)   # python builds must authorize the load first
                sqlite_vec.load(self._db)
                self._db.enable_load_extension(False)
                with self._lock:
                    self._adopt_or_create_vec_table(self._vec_dimension())
                    self._db.commit()
                if not self._vec_ok:
                    return   # dimension mismatch — honest gap already recorded
                _LOG.info("base-db vec engine ready (%s)", self._EMBED_MODEL)
            except (sqlite3.OperationalError, AttributeError) as exc:
                # build without extension support / locked loader — honest degrade, never crash the floor
                self._vec_ok = False
                self._vec_gap = "extension load failed: %s" % exc
                _LOG.warning("base-db vec engine degraded to fts-only: %s", exc)

    def _fts_rebuild(self):
        """Index refresh in a pass (after each write batch), NOT per trigger — the episodic law."""
        assert self._db is not None
        self._db.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
        self._db.execute("INSERT INTO pointers_fts(pointers_fts) VALUES('rebuild')")

    # ── docked contract: write / search / touch ──
    def write(self, kind, content, tags=None, target_hint=None, session_id=""):
        """Store one fact row. Dedupe over sha256(content) — idempotent, replayable.
        Legacy vocabulary: the kind "curation" is tolerated and stored as "curated"
        (the write-kind vocabulary is explicit|curated only)."""
        assert self._db is not None, "not initialized"
        if kind == "curation":      # legacy vocabulary (A-3) — normalize, never store it
            kind = "curated"
        content = (content or "").strip()
        if not content:
            raise ValueError("empty content")
        hash_ = hashlib.sha256(content.encode("utf-8")).hexdigest()
        meta = json.dumps({"tags": tags or []}, ensure_ascii=False)
        now = _utcnow_iso()
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO facts(ts, kind, content, target, source, session_id,"
                " hash, hits, meta) VALUES(?,?,?,?,?,?,?,0,?)",
                (now, kind, content, target_hint, "write", session_id, hash_, meta))
            if cur.rowcount == 0:  # dedupe hit — existing row wins, nothing duplicated
                row = self._db.execute(
                    "SELECT id, target, ts FROM facts WHERE hash=?", (hash_,)).fetchone()
                return {"status": "duplicate", "id": row[0],
                        "target": row[1] or "base-db:%d" % row[0], "ts": row[2]}
            rowid = cur.lastrowid
            target = target_hint or "base-db:%d" % rowid
            self._db.execute("UPDATE facts SET target=? WHERE id=?", (target, rowid))
            # hot path: incrementally index the ONE new row (external-content FTS5);
            # the full 'rebuild' pass stays on cold paths (remove/pointer) only
            self._db.execute("INSERT INTO facts_fts(rowid, content) VALUES(?, ?)",
                             (rowid, content))
            self._db.commit()
        return {"status": "stored", "id": rowid, "target": target, "ts": now}

    def write_pointer(self, subject, location, kind=None):
        """Directory row: WHAT → WHERE. Auto from route log/curation, never auto from content."""
        assert self._db is not None, "not initialized"
        subject, location = (subject or "").strip(), (location or "").strip()
        if not (subject and location):
            raise ValueError("pointer needs subject AND location")
        now = _utcnow_iso()
        with self._lock:
            self._db.execute(
                "INSERT INTO pointers(subject, location, kind, ts, checked)"
                " VALUES(?,?,?,?,'')", (subject, location, kind, now))
            self._fts_rebuild()
            self._db.commit()


    # ── vec engine (optional upgrade; the degradation law degradation ladder) ──
    def card_modes(self):
        """DYNAMIC modes: the card the provider shows reflects the environment honestly."""
        return ("fts", "pointers") + (("vec",) if self._vec_ok else ())

    def _read_vec_batch(self) -> int:
        """Backfill batch from the provider config (wizard writes strings) — default 32."""
        try:
            batch = int(self._provider_config.get("vec_backfill_batch") or 32)
        except (TypeError, ValueError):
            batch = 32
        return max(1, batch)

    def _vec_dimension(self):
        """Derive the embedding dimension from ONE model probe — the table is sized by
        the CONFIGURED model, never by a hard-coded default. A probe failure degrades
        honestly (gap + no vec); it does not fall back to a guessed dimension."""
        try:
            vecs = self._embed(["memrack dimension probe"])
        except Exception as exc:
            self._vec_gap = "dimension probe failed: %s: %s" % (type(exc).__name__, exc)
            return None
        if not vecs or not vecs[0]:
            self._vec_gap = "dimension probe returned no embedding"
            return None
        return len(vecs[0])

    def _adopt_or_create_vec_table(self, dim):
        """Fresh store: create facts_vec at the probed dimension. Existing store: sanity-check
        the table against the probed model — a row-backed mismatch degrades AT INIT with an
        honest gap instead of failing inserts silently at search time."""
        assert self._db is not None
        if dim is None:
            self._vec_ok = False
            return
        exists = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts_vec'").fetchone()
        if exists is None:
            self._db.executescript(
                "CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(embedding float[%d]);" % dim)
            return
        existing = self._db.execute(
            "SELECT vec_length(embedding) FROM facts_vec LIMIT 1").fetchone()
        if existing and existing[0] is not None and int(existing[0]) != dim:
            self._vec_ok = False
            self._vec_gap = ("vec dimension mismatch: facts_vec expects %s, "
                             "model %r produces %d" % (existing[0], self._EMBED_MODEL, dim))
            _LOG.warning("base-db vec engine degraded to fts-only: %s", self._vec_gap)

    def _vec_probe(self):
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            self._vec_gap = "sqlite-vec package not installed (optional dep)"
            return False
        try:
            import urllib.request, json as _json
            with urllib.request.urlopen(
                    self._OLLAMA_HOST.rstrip("/") + "/api/tags", timeout=1) as resp:
                tags = _json.load(resp)
        except Exception as exc:
            self._vec_gap = "%s not reachable: %s" % (self._OLLAMA_HOST, exc)
            return False
        names = [str(m.get("name", "")).split(":")[0] for m in tags.get("models", [])]
        if self._EMBED_MODEL not in names:
            self._vec_gap = "embedding model %r not pulled (ollama list)" % self._EMBED_MODEL
            return False
        return True

    def _embed(self, texts):
        import urllib.request, json as _json
        body = _json.dumps({"model": self._EMBED_MODEL, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(
            self._OLLAMA_HOST.rstrip("/") + "/api/embed", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.load(resp)
        return list(data.get("embeddings") or [])

    def _vec_index_missing(self):
        """Index-refresh pass, part 1: embed facts lacking a vector — incremental, batched.
        Returns (indexed, backlog): backlog = rows STILL missing a vector after this batch,
        surfaced as a note (no silent incompleteness impression)."""
        import sqlite_vec
        assert self._db is not None
        with self._lock:
            backlog = self._db.execute(
                "SELECT COUNT(*) FROM facts f LEFT JOIN facts_vec v ON v.rowid = f.id"
                " WHERE v.rowid IS NULL").fetchone()[0]
            if not backlog:
                return 0, 0
            rows = self._db.execute(
                "SELECT f.id, f.content FROM facts f LEFT JOIN facts_vec v ON v.rowid = f.id"
                " WHERE v.rowid IS NULL ORDER BY f.id LIMIT ?", (self._vec_batch,)).fetchall()
        ids = [r[0] for r in rows]
        vecs = self._embed([r[1] for r in rows])       # network call OUTSIDE the lock
        if len(vecs) != len(rows):
            raise RuntimeError("embedding count mismatch: %d != %d" % (len(vecs), len(rows)))
        try:
            with self._lock:
                self._db.executemany(
                    "INSERT INTO facts_vec(rowid, embedding) VALUES(?, ?)",
                    [(rid, sqlite_vec.serialize_float32(v)) for rid, v in zip(ids, vecs)])
                self._db.commit()
        except sqlite3.OperationalError as exc:
            # e.g. a dimension mismatch against the model — honest degrade, never a silent fail
            self._vec_ok = False
            self._vec_gap = "vec insert failed (%s): %s" % (type(exc).__name__, exc)
            _LOG.warning("base-db vec engine degraded to fts-only: %s", exc)
            raise ValueError("vec disabled: %s" % self._vec_gap) from exc
        return len(rows), backlog - len(rows)

    def _vec_knn(self, query, k):
        import sqlite_vec
        q_vecs = self._embed([query])                  # network call OUTSIDE the lock
        assert self._db is not None, "not initialized"
        if not q_vecs:
            return []
        qvec = sqlite_vec.serialize_float32(q_vecs[0])
        hits = []
        with self._lock:
            rows = self._db.execute(
                "SELECT rowid, distance FROM facts_vec WHERE embedding MATCH ? AND k = ?",
                (qvec, k)).fetchall()
            for i, (rid, dist) in enumerate(rows, 1):
                row = self._db.execute(
                    "SELECT content, target, ts, hits FROM facts WHERE id=?", (rid,)).fetchone()
                if row:
                    # `id` = the fact rowid: the provider's touch burns HEAT into semantic hits
                    hits.append({"backend": self.name, "kind": "fact", "id": rid, "rank": i,
                                 "content": row[0],
                                 "target": row[1] or "base-db:%d" % rid, "ts": row[2],
                                 "hits": row[3],
                                 "distance": round(float(dist), 4)})
        return hits

    def search(self, query, mode, k):
        """Ranked hits per the retrieval tools.2. 'fts' = facts, 'pointers' = directory. No 'vec' yet."""
        assert self._db is not None, "not initialized"
        if mode == "fts":
            rows, src = self._fts_query(query, k, "facts_fts",
                                        "SELECT facts.id, facts.content AS content,"
                                        " facts.target AS target, facts.ts AS ts, facts.hits AS hits FROM facts_fts"
                                        " JOIN facts ON facts.id = facts_fts.rowid"
                                        " WHERE facts_fts MATCH ?", ("content",))
            hits = [{"backend": self.name, "kind": "fact", "id": r[0], "rank": i + 1,
                     "content": r[1], "target": r[2] or "base-db:%d" % r[0], "ts": r[3],
                     "hits": r[4]}
                    for i, r in enumerate(rows)]
        elif mode == "pointers":
            rows, _ = self._fts_query(query, k, "pointers_fts",
                                      "SELECT pointers_fts.subject AS subject,"
                                      " pointers_fts.location AS location,"
                                      " pointers.kind AS kind, pointers.ts AS ts FROM pointers_fts"
                                      " JOIN pointers ON pointers.id = pointers_fts.rowid"
                                      " WHERE pointers_fts MATCH ?", ("subject", "location"))
            hits = [{"backend": self.name, "kind": "pointer", "rank": i + 1,
                     "content": "%s -> %s" % (r[0], r[1]), "target": "pointer:%s" % r[1],
                     "ts": r[3]} for i, r in enumerate(rows)]
        elif mode == "vec":
            if not self._vec_ok:
                raise ValueError("vec not available: %s" % self._vec_gap)
            indexed, backlog = self._vec_index_missing()   # inline pass on the first vec query
            hits = self._vec_knn(query, k)
            if backlog:                        # the honest incompleteness note on every hit
                for h in hits:
                    h["_vec_backlog"] = backlog
            return hits
        else:
            raise ValueError("this engine carries mode %r not (modes: %s)"
                             % (mode, list(self.card_modes())))
        return hits

    def _fts_query(self, query, k, table, sql, cols):
        assert self._db is not None
        query = (query or "").strip()
        order = " ORDER BY %s.rank LIMIT ?" % table
        try:
            with self._lock:
                rows = self._db.execute(sql + order, (self._safe_match(query, cols), k)).fetchall()
            return rows, True
        except sqlite3.OperationalError as exc:
            # malformed MATCH syntax — fall back to a quoted whole phrase, never crash
            # the search. The fallback fragment is built DIRECTLY here: routing it
            # through _safe_match again would strip its quotes and hand the raw
            # colon query right back into passthrough (T-029 bug 1 — both tiers
            # failing on one and the same expression).
            _LOG.warning("memrack base-db fts failed on %r: %s — trying quoted phrase", query, exc)
            phrase = '"%s"' % query.replace('"', "")
            with self._lock:
                try:
                    rows = self._db.execute(sql + order, (phrase, k)).fetchall()
                except sqlite3.OperationalError as exc2:
                    _LOG.error("memrack base-db fts unusable: %s", exc2)
                    return [], False
            return rows, True

    @staticmethod
    def _safe_match(query, cols):
        """fts5 MATCH builder: tokens get QUOTED (hyphens AND colons are fts5 syntax —
        fts5 reads 'Hit-me' as 'no such column: me', '10:30' as 'no such column: 10').
        Advanced passthrough ONLY when the query carries a REAL column filter — a
        '^word:' prefix naming one of `cols`. Plain queries with stray colons
        (times, URLs) are tokenized and quoted instead of riding raw (T-029 bug 1)."""
        m = (query or "").strip().replace('"', "")
        pm = re.match(r"(\w+):", m)
        if pm and pm.group(1) in tuple(cols):
            return m            # real column passthrough (subject:x) — the agent knows what it is doing
        tokens = [t for t in re.split(r"\W+", m) if t]
        if not tokens:
            return '"%" '
        if len(cols) > 1:
            return " OR ".join('%s:(%s)' % (c, " ".join('"%s"' % t for t in tokens)) for c in cols)
        return " OR ".join('"%s"' % t for t in tokens)   # OR + rank: long queries keep recall

    def touch(self, ids):
        """Heat signal (the fusion law): hits++ on returned fact rows — report basis, no trigger."""
        assert self._db is not None
        ids = [i for i in ids if isinstance(i, int)]
        if not ids:
            return
        with self._lock:
            self._db.executemany("UPDATE facts SET hits=hits+1 WHERE id=?",
                                 [(i,) for i in ids])
            self._db.commit()

    # ── provider hooks ──
    def on_turn(self, user, assistant, *, session_id=""):
        """Turns are NOT copied into facts (episodic lives in Hermes' state.db — the episodic law)."""
        return None

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        # S-3 law: the target identifies THE ENTRY, not the file — every mirrored write gets
        # a per-entry target (hash8 suffix, same convention as the legacy import). A shared
        # target would let the RRF merge collapse N entries into one and a remove sweep
        # every sibling away.
        base_target = "builtin:%s" % (target or "memory")
        # the runtime mirrors replace/patch/remove with the FULL previous entry in metadata
        previous = str((metadata or {}).get("previous_content") or "").strip()
        if action in ("add", "patch", "replace"):
            if previous and action in ("patch", "replace"):
                # patch/replace RESPECT previous_content: the old entry goes out first,
                # the edited entry arrives as the fresh per-entry-target row
                self._remove_facts(previous, base_target)
            return self.write("explicit", content,
                              tags=[action] if action not in ("add",) else None,
                              target_hint="%s#%s" % (base_target, hashlib.sha256(
                                  (content or "").encode("utf-8")).hexdigest()[:8]),
                              session_id=session_id)
        if action == "remove":
            return self._remove_facts(previous or (content or "").strip(), base_target)
        return {"status": "ignored", "reason": "unknown action %r" % action}

    def _remove_facts(self, needle, base_target):
        """Remove by EXACT identity first (dedupe hash of the needle OR the per-entry
        target). A needle that matches nothing falls back to a substring search
        with an HONEST report (what went, how many, which match) — never a
        silent no-op. The containment law keeps the fallback SAFE: ONE
        substring hit removes as before; MULTIPLE hits remove NOTHING and
        return `status: ambiguous` with the candidates (id/target/snippet)
        for a targeted choice. Every delete prunes facts_vec (its rowid IS
        the fact id it indexes — stale vectors on a re-used id would hand
        back the WRONG embedding)."""
        needle = (needle or "").strip()
        if not needle:
            return {"status": "not_found", "needle": needle, "match": "none",
                    "removed": 0, "rows": []}
        assert self._db is not None
        needle_hash = hashlib.sha256(needle.encode("utf-8")).hexdigest()
        precise_target = "%s#%s" % (base_target, needle_hash[:8])
        with self._lock:
            rows = self._db.execute(
                "SELECT id, target, content FROM facts WHERE hash=? OR target=?",
                (needle_hash, precise_target)).fetchall()
            match, gone = "exact", [(r[0], r[1], r[2]) for r in rows]
            if not gone:
                rows = self._db.execute(
                    "SELECT id, target, content FROM facts WHERE instr(content, ?) > 0",
                    (needle,)).fetchall()
                match = "substring"
                gone = [(r[0], r[1], r[2]) for r in rows]
                if len(gone) > 1:
                    # the containment law: a short or partial needle must
                    # never swallow several rows in one sweep — the fallback
                    # stays a CANDIDATE search, nothing is deleted and the
                    # callers get the targeted picks (id/target/snippet)
                    candidates = [{"id": i, "target": t or "base-db:%d" % i,
                                   "snippet": c[:80]} for i, t, c in gone]
                    return {"status": "ambiguous", "needle": needle,
                            "match": "substring", "removed": 0, "rows": [],
                            "candidates": candidates}
            if gone:
                self._db.executemany("DELETE FROM facts WHERE id=?", [(i,) for i, _t, _c in gone])
                self._prune_facts_vec([i for i, _t, _c in gone])
                self._fts_rebuild()      # cold path: rebuild after a batch delete
                self._db.commit()
        report_rows = [{"target": t or "base-db:%d" % i, "content": c[:80]}
                       for i, t, c in gone]
        if not gone:
            return {"status": "not_found", "needle": needle, "match": match,
                    "removed": 0, "rows": []}
        return {"status": "removed", "needle": needle, "match": match,
                "removed": len(gone), "rows": report_rows}

    def _prune_facts_vec(self, ids):
        """Vector cleanup at delete — the fix for fact-id reuse on stores created BEFORE
        the AUTOINCREMENT schema (works on every existing store). Best-effort: the table
        exists only where the vec engine ever initialized; there is no fact_id column —
        the vec0 rowid IS the fact id."""
        assert self._db is not None
        if not ids:
            return
        if self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts_vec'").fetchone():
            self._db.executemany("DELETE FROM facts_vec WHERE rowid=?", [(i,) for i in ids])

    def on_session_end(self, messages, *, session_id=""):
        """No automated consolidation here (the dreaming lesson is the project law)."""
        return None

    def prefetch(self, query, *, session_id=""):
        """No search noise in prefetch (the prefetch law) — the card arrives at the provider."""
        return ""

    def system_prompt_block(self):
        return ""

    def shutdown(self):
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    # ── inventory ──
    @property
    def db_path(self):
        return self._path

    def export(self):
        assert self._db is not None or self._path is not None
        facts = pointers = None
        if self._db is not None:
            with self._lock:               # shared connection — reads lock like the writes
                f = self._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                p = self._db.execute("SELECT COUNT(*) FROM pointers").fetchone()[0]
            facts, pointers = f, p
        return {"name": self.name, "best_for": self.best_for, "limits": self.limits,
                "search": list(self.card_modes()), "write": self.writeable,
                "built_in": True, "facts": facts, "pointers": pointers,
                "vec": self._vec_ok, "vec_gap": None if self._vec_ok else self._vec_gap,
                "db": str(self._path) if self._path else None}


def export_backend():
    return BaseDbBackend()
