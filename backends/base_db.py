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


class BaseDbBackend:
    name = PLUGIN_NAME
    best_for = "atomic facts + mirror of memory-writes; exact recall in milliseconds"
    limits = "ONE sentence per row on write; import allows multi-sentence legacy rows (tags import:)"
    search_modes = ("fts", "pointers")   # STATIC floor; card_modes() reports the DYNAMIC set
    writeable = True
    # the write-kind vocabulary — legacy input "curation" is tolerated and normalized to "curated"
    write_types = ("explicit", "curated")

    def __init__(self):
        self._db: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._lock = threading.Lock()
        self._provider_config: dict = {}
        pass

    # ── lifecycle ──
    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        self._provider_config = dict(provider_config or {})
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


    def card_modes(self):
        return ("fts", "pointers")

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
                self._fts_rebuild()      # cold path: rebuild after a batch delete
                self._db.commit()
        report_rows = [{"target": t or "base-db:%d" % i, "content": c[:80]}
                       for i, t, c in gone]
        if not gone:
            return {"status": "not_found", "needle": needle, "match": match,
                    "removed": 0, "rows": []}
        return {"status": "removed", "needle": needle, "match": match,
                "removed": len(gone), "rows": report_rows}

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
                "db": str(self._path) if self._path else None}


def export_backend():
    return BaseDbBackend()
