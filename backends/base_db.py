"""Built-in "base-db" backend — the stdlib floor of the rack.

SQLite-only store (facts + pointers + FTS5), zero external dependencies:
no vector engine, no network, no wiki requirement. Works solo — one backend
is the whole rack (Basisfall, SPEC §1.4). The vec upgrade (T-007) slots in
without touching this contract.

Card (code-first built-in; docked backends carry theirs in the manifest):
    name:    base-db
    best_for: atomare Fakten + Mirror von memory-writes; exakte Nachfrage, Millisekunden
    limits:  EIN Satz pro Row, keine Prosa; kein vec
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

PLUGIN_NAME = "base-db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id      INTEGER PRIMARY KEY,
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


_VEC_TABLE = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(embedding float[768]);
"""


class BaseDbBackend:
    name = PLUGIN_NAME
    best_for = "atomare Fakten + Mirror von memory-writes; exakte Nachfrage in Millisekunden"
    limits = "EIN Satz pro Row, keine Prosa; kein Prosa-Store; vec nur wenn sqlite-vec+ollama ver\u00fcgbar"
    search_modes = ("fts", "pointers")   # STATIC floor; card_modes() reports the DYNAMIC set
    writeable = True
    write_types = ("explicit", "episodic", "semantic", "curation")  # accepts everything routed at it

    _EMBED_MODEL = "nomic-embed-text"
    _OLLAMA_HOST = "http://127.0.0.1:11434"

    def __init__(self):
        self._db: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._lock = threading.Lock()
        self._provider_config: dict = {}
        self._vec_ok = False
        self._vec_gap = "not probed yet"

    # ── lifecycle ──
    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        self._provider_config = dict(provider_config or {})
        self._EMBED_MODEL = self._provider_config.get("embedding_model") or self._EMBED_MODEL
        self._OLLAMA_HOST = self._provider_config.get("ollama_host") or self._OLLAMA_HOST
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
        # vec capability probe: optional upgrade (T-007) — degrade to fts-only with a visible gap
        self._vec_ok = self._vec_probe()
        if self._vec_ok:
            try:
                import sqlite_vec
                self._db.enable_load_extension(True)   # python builds must authorize the load first
                sqlite_vec.load(self._db)
                self._db.enable_load_extension(False)
                with self._lock:
                    self._db.executescript(_VEC_TABLE)
                    self._db.commit()
                _LOG.info("base-db vec engine ready (%s)", self._EMBED_MODEL)
            except (sqlite3.OperationalError, AttributeError) as exc:
                # build without extension support / locked loader — honest degrade, never crash the floor
                self._vec_ok = False
                self._vec_gap = "extension load failed: %s" % exc
                _LOG.warning("base-db vec engine degraded to fts-only: %s", exc)

    def _fts_rebuild(self):
        """Index-Refresh im Pass (nach jedem Write-Batch), NICHT per Trigger — SPEC §2."""
        assert self._db is not None
        self._db.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
        self._db.execute("INSERT INTO pointers_fts(pointers_fts) VALUES('rebuild')")

    # ── docked contract: write / search / touch ──
    def write(self, kind, content, tags=None, target_hint=None, session_id=""):
        """Store one fact row. Dedupe over sha256(content) — idempotent, replayable."""
        assert self._db is not None, "not initialized"
        content = (content or "").strip()
        if not content:
            raise ValueError("empty content")
        hash_ = hashlib.sha256(content.encode("utf-8")).hexdigest()
        meta = json.dumps({"tags": tags or []}, ensure_ascii=False)
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
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
            self._fts_rebuild()
            self._db.commit()
        return {"status": "stored", "id": rowid, "target": target, "ts": now}

    def write_pointer(self, subject, location, kind=None):
        """Verzeichnis-Row: WAS → WO. Auto aus Route-Log/Kuratur, nie auto aus Inhalten."""
        assert self._db is not None, "not initialized"
        subject, location = (subject or "").strip(), (location or "").strip()
        if not (subject and location):
            raise ValueError("pointer needs subject AND location")
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        with self._lock:
            self._db.execute(
                "INSERT INTO pointers(subject, location, kind, ts, checked)"
                " VALUES(?,?,?,?,'')", (subject, location, kind, now))
            self._fts_rebuild()
            self._db.commit()


    # ── vec engine (optional upgrade; SPEC §1.4 degradation ladder) ──
    def card_modes(self):
        """DYNAMIC modes: the card the provider shows reflects the environment honestly."""
        return ("fts", "pointers") + (("vec",) if self._vec_ok else ())

    def _vec_probe(self):
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            self._vec_gap = "sqlite-vec package not installed (optional dep)"
            return False
        try:
            import urllib.request, json as _json
            with urllib.request.urlopen(
                    self._OLLAMA_HOST.rstrip("/") + "/api/tags", timeout=3) as resp:
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

    def _vec_index_missing(self, batch=32):
        """Index-Refresh-Pass, Teil 1: facts ohne Vektor einbetten — inkrementell, batched."""
        import sqlite_vec
        rows = self._db.execute(
            "SELECT f.id, f.content FROM facts f LEFT JOIN facts_vec v ON v.rowid = f.id"
            " WHERE v.rowid IS NULL ORDER BY f.id LIMIT ?", (batch,)).fetchall()
        if not rows:
            return 0
        ids = [r[0] for r in rows]
        vecs = self._embed([r[1] for r in rows])
        if len(vecs) != len(rows):
            raise RuntimeError("embedding count mismatch: %d != %d" % (len(vecs), len(rows)))
        with self._lock:
            self._db.executemany(
                "INSERT INTO facts_vec(rowid, embedding) VALUES(?, ?)",
                [(rid, sqlite_vec.serialize_float32(v)) for rid, v in zip(ids, vecs)])
            self._db.commit()
        return len(rows)

    def _vec_knn(self, query, k):
        import sqlite_vec
        q_vecs = self._embed([query])
        if not q_vecs:
            return []
        qvec = sqlite_vec.serialize_float32(q_vecs[0])
        rows = self._db.execute(
            "SELECT rowid, distance FROM facts_vec WHERE embedding MATCH ? AND k = ?",
            (qvec, k)).fetchall()
        hits = []
        for i, (rid, dist) in enumerate(rows, 1):
            row = self._db.execute(
                "SELECT content, target, ts FROM facts WHERE id=?", (rid,)).fetchone()
            if row:
                hits.append({"backend": self.name, "kind": "fact", "rank": i,
                             "content": row[0],
                             "target": row[1] or "base-db:%d" % rid, "ts": row[2],
                             "distance": round(float(dist), 4)})
        return hits

    def search(self, query, mode, k):
        """Ranked hits per SPEC §3.2. 'fts' = facts, 'pointers' = Verzeichnis. No 'vec' yet."""
        assert self._db is not None, "not initialized"
        if mode == "fts":
            rows, src = self._fts_query(query, k, "facts_fts",
                                        "SELECT facts.id, facts.content AS content,"
                                        " facts.target AS target, facts.ts AS ts FROM facts_fts"
                                        " JOIN facts ON facts.id = facts_fts.rowid"
                                        " WHERE facts_fts MATCH ?", ("content",))
            hits = [{"backend": self.name, "kind": "fact", "rank": i + 1,
                     "content": r[1], "target": r[2] or "base-db:%d" % r[0], "ts": r[3]}
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
            self._vec_index_missing()          # inline pass: backlog embeds on first vec query
            return self._vec_knn(query, k)
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
            # malformed MATCH syntax — fall back to a quoted whole phrase, never crash the search;
            # a SECOND failure is diagnostic gold — log it, never swallow silently
            _LOG.warning("memrack base-db fts failed on %r: %s — trying quoted phrase", query, exc)
            with self._lock:
                try:
                    rows = self._db.execute(
                        sql + order,
                        (self._safe_match('"%s"' % query.replace('"', ""), cols), k)).fetchall()
                except sqlite3.OperationalError as exc2:
                    _LOG.error("memrack base-db fts unusable: %s", exc2)
                    return [], False
            return rows, True

    @staticmethod
    def _safe_match(query, cols):
        """fts5 MATCH builder: tokens werden QUOTED (Hyphen sind in fts5 Column-Syntax —
        'Hit-me' liest fts5 als 'no such column: me'). Advanced-Syntax (Col:-Filter) bleibt
        Passthrough, wenn der Query selbst einen ':' trägt."""
        m = (query or "").strip()
        m = m.replace('"', "")
        if ":" in m:            # advanced passthrough (subject:x) — Agent weiß, was er tut
            return m
        tokens = [t for t in re.split(r"\W+", m) if t]
        if not tokens:
            return '"%" '
        joiner = " OR " if len(cols) > 1 else " "
        if len(cols) > 1:
            return " OR ".join('%s:(%s)' % (c, " ".join('"%s"' % t for t in tokens)) for c in cols)
        return " ".join('"%s"' % t for t in tokens)

    def touch(self, ids):
        """Heat-Signal (SPEC §3.3): hits++ auf returned fact rows — Report-Basis, kein Trigger."""
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
        """Turns are NOT copied into facts (episodic lives in Hermes' state.db — SPEC §2)."""
        return None

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        if action in ("add", "patch", "replace"):
            return self.write("explicit", content,
                              tags=[action] if action not in ("add",) else None,
                              target_hint="builtin:%s" % (target or "memory"),
                              session_id=session_id)
        if action == "remove":
            assert self._db is not None
            with self._lock:
                self._db.execute("DELETE FROM facts WHERE content=? OR target=?",
                                 ((content or "").strip(), "builtin:%s" % (target or "memory")))
                self._fts_rebuild()
                self._db.commit()
            return {"status": "removed", "needle": content}
        return {"status": "ignored", "reason": "unknown action %r" % action}

    def on_session_end(self, messages, *, session_id=""):
        """Keine Konsolideierung hier automatisiert (Dreaming-Lektion, SPEC §4)."""
        return None

    def prefetch(self, query, *, session_id=""):
        """Kein Search-Noise im prefetch (SPEC §3.5) — die Card kommt beim Provider."""
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
