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


class BaseDbBackend:
    name = PLUGIN_NAME
    best_for = "atomare Fakten + Mirror von memory-writes; exakte Nachfrage in Millisekunden"
    limits = "EIN Satz pro Row, keine Prosa; kein vec (optionaler Upgrade T-007)"
    search_modes = ("fts", "pointers")   # vec = optionaler Upgrade (T-007), dann hier ergänzt
    writeable = True
    write_types = ("explicit", "episodic", "semantic", "curation")  # accepts everything routed at it

    def __init__(self):
        self._db: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._lock = threading.Lock()

    # ── lifecycle ──
    def initialize(self, session_id="", hermes_home=None, **kwargs):
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
        else:
            raise ValueError("this engine does not carry mode %r (modes: %s)" % (mode, list(self.search_modes)))
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
                "search": list(self.search_modes), "write": self.writeable,
                "built_in": True, "facts": facts, "pointers": pointers,
                "db": str(self._path) if self._path else None}


def export_backend():
    return BaseDbBackend()
