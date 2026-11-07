"""TEMPLATE — copy me (out of the repo!) to $HERMES_HOME/plugins/memrack/backends.d/

Minimal, self-contained backend: card as class attrs, two real methods.
NO SDK requirement: this file runs standalone (the SDK is optional sugar).
Dock via a manifest beside it:

    name: mein-archiv
    module: meine_backend_datei.py
    best_for: ...
    limits: ...
    search: [fts]

`rm` the manifest = registered-removed; `disabled-` prefix = parked.
"""

import sqlite3
import threading

def _utcnow_iso():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
from pathlib import Path

# SDK: OPTIONAL. If the sdk is importable, inherit the lessons; else pure duck-type.
try:
    from memrack.sdk import MemrackBackend, fts_match_tokens
except ImportError:
    class MemrackBackend:            # duck-full: ALL contract hooks as no-ops
        def on_turn(self, user, assistant, *, session_id=""): return None
        def on_memory_write(self, action, target, content, metadata=None, *, session_id=""): return None
        def on_session_end(self, messages, *, session_id=""): return None
        def prefetch(self, query, *, session_id=""): return ""
        def system_prompt_block(self): return ""
        def shutdown(self): pass
        def card_modes(self): return tuple(self.search_modes)
    def fts_match_tokens(query, *, columns=None):
        tokens = [t for t in __import__("re").split(r"\W+", (query or "").strip().replace('"', "")) if t]
        quoted = ['"%s"' % t for t in tokens]
        return " ".join(quoted) if not columns else \
            " OR ".join('%s:(%s)' % (c, " ".join(quoted)) for c in columns)


class TemplateBackend(MemrackBackend if MemrackBackend is not object else object):
    name = "template"                       # UNIQUE in the rack
    best_for = "example: atomic facts from a foreign tool"
    limits = "example: read-only mirror; no vec"
    search_modes = ("fts",)                 # claim what search() really implements
    writeable = True

    def __init__(self):
        if hasattr(MemrackBackend, "initialize"):
            super().__init__()      # SDK-Zucker (seine RLock-Disziplin)
        self._lock = threading.RLock()
        self._db = None

    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        base = Path(hermes_home or Path.home() / ".hermes") / "plugin-data" / "memrack"
        base.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(base / "template-backend.db"),
                                   check_same_thread=False, timeout=5.0)
        # UNIQUE = die Ledger-Idempotenz (kein Doppel-Row bei gleichem Inhalt);
        # ts = UTC-Z-µs (Gesetzt 1); hits = Heat (Gesetze 2: fact-hits TRAGEN hits)
        self._db.execute("CREATE TABLE IF NOT EXISTS notes"
                         "(id INTEGER PRIMARY KEY, text TEXT, hash TEXT UNIQUE,"
                         " ts TEXT, hits INTEGER DEFAULT 0)")
        self._db.commit()

    def search(self, query, mode, k, session_id=""):
        if mode != "fts":
            raise ValueError("template carries fts only")
        rows = self._db.execute(
            "SELECT id, text, ts, hits FROM notes").fetchall()
        tokens = [t.lower() for t in fts_match_tokens(query).replace('"', '').split()]
        # Hit = die GANZE Row-Referenz (kind=fact: content klein ok); hits/ts = Gesetze 1+2
        ranked = [({"backend": self.name, "kind": "fact", "rank": i,
                    "content": text, "target": "template:%d" % rid,
                    "ts": ts, "hits": hits})
                  for i, (rid, text, ts, hits) in enumerate(
                      [r for r in rows if any(t in r[1].lower() for t in tokens)], 1)]
        return ranked[:k]

    def touch(self, ids):
        """Gesetzt 2 die andere Richtung: hit-rauf nach der Merge-Auslieferung."""
        if not ids:
            return
        with self._lock:
            self._db.executemany("UPDATE notes SET hits = hits + 1 WHERE id = ?",
                                 [(i,) for i in ids])
            self._db.commit()

    def write(self, kind, content, tags=None, session_id=""):
        import hashlib
        hash_ = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO notes(text, hash, ts) VALUES (?, ?, ?)",
                (content, hash_, _utcnow_iso()))
            if cur.rowcount == 0:   # dedupe: bestehend gewinnt, kein Doppel-Row
                rid = self._db.execute("SELECT id FROM notes WHERE hash=?", (hash_,)).fetchone()[0]
                return {"status": "duplicate", "id": rid, "target": "template:%d" % rid}
            rid = cur.lastrowid
            self._db.commit()
        return {"status": "stored", "id": rid, "target": "template:%d" % rid}

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        if action in ("add", "patch", "replace"):
            return self.write("explicit", content, session_id=session_id)

    def export(self):
        n = (self._db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
             if self._db else 0)
        return {"name": self.name, "best_for": self.best_for, "limits": self.limits,
                "search": list(self.card_modes()), "write": self.writeable, "n": n}


def export_backend():
    """REQUIRED: the docking contract — the registry/conformance need exactly this."""
    return TemplateBackend()
