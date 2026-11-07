"""Built-in "state-episodic" backend — READ-ONLY search engine over the session history.

The state.db stays CANONICAL and UNTOUCHED (SPEC law: episodic lives in Hermes
state.db — turns are never copied). This backend opens it SQLITE READ-ONLY
(`mode=ro` — no writes, no triggers, no WAL pokes of its own) and exposes the
'episodic' search mode so memrack_search fan-out covers past conversations
automatically: same pattern as the wiki engine, a live scan of a foreign store.

Hit = kind "episodic", target `session:<sid>#<msg-id>` (a reference — the whole
turn lives in state.db; the agent re-reads via session ids), snippet via fts5
snippet(), ts normalized from the epoch-float to the house norm: UTC-Z
microseconds (ts law). No heat: writeless backend (heat obligate is fact-only).
"""

import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger("memrack.state-episodic")


def _iso_from_epoch(epoch) -> str:
    """state.db stores epoch-float timestamps — the merge compares ts STRINGS,
    so foreign formats get normalized to UTC-Z µs (A-5 law)."""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError, OSError):
        return "1970-01-01T00:00:00.000000Z"


class StateEpisodicBackend:
    name = "state-episodic"
    best_for = "past session chats and tool results (state.db, READ-ONLY): who/when/what happened"
    limits = "read-only \u2014 no write/heat; FTS over messages_fts + session titles; the state.db is and stays canonical"
    search_modes = ("episodic",)
    writeable = False

    def __init__(self):
        self._db: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._locked = threading.RLock()

    # ── lifecycle ──
    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        home = Path(hermes_home or (Path.home() / ".hermes"))
        self._path = home / "state.db"
        if not self._path.exists():
            raise RuntimeError("state.db not present \u2014 the Basisfall without sessions is no error here;"
                               " the backend must refuse to load (gap-note: no state.db)")
        try:
            self._db = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True,
                                       check_same_thread=False, timeout=5.0)
        except sqlite3.Error as e:
            raise RuntimeError(f"state.db read-only attach failed: {e}") from e
        # schema verification: without the FTS table (older Hermes) = honestly no episodic value
        try:
            self._db.execute("SELECT rowid FROM messages_fts LIMIT 1").fetchone()
        except sqlite3.OperationalError as e:
            self._db.close(); self._db = None  # release, ro-mode touched nothing
            raise RuntimeError(f"messages_fts missing (older state.db): {e}") from e
        _LOG.info("state-episodic attached (READ-ONLY): %s", self._path)

    # ── search engine ──
    def search(self, query, mode, k, session_id=""):
        """episodic hits: fts5 MATCH over content/tool_name, each token QUOTED (hyphen trap),
        JOIN sessions for title + rank — ts via ISO norm."""
        assert self._db is not None, "not initialized"
        if mode != "episodic":
            raise ValueError("state-episodic carries mode 'episodic' only (got %r)" % mode)
        tokens = [t for t in re.split(r"\W+", (query or "").strip().replace('"', "")) if t]
        if not tokens:
            return []
        quoted = " OR ".join(['"%s"' % t for t in tokens])   # OR: hybrid recall beats AND-gate here
        sql = ("SELECT m.id, m.session_id, m.role, m.timestamp,"
               " snippet(messages_fts, 0, '<mark>', '</mark>', ' … ', 24) AS snip"
               " FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
               " WHERE messages_fts MATCH ? AND m.active = 1"
               " ORDER BY bm25(messages_fts) LIMIT ?")
        with self._locked:
            rows = self._db.execute(sql, (quoted, k)).fetchall()
        return [{"backend": self.name, "kind": "episodic", "rank": i + 1,
                 "content": ("%s: %s" % (r[2], r[4]))[:320],        # role + hit line
                 "target": "session:%s#%d" % (r[1], r[0]),           # REFERENCE: the state.db holds the turn
                 "ts": _iso_from_epoch(r[3]),
                 "snippet": r[4]}
                for i, r in enumerate(rows)]

    # ── provider hooks (writeless readback) ──
    def on_turn(self, user, assistant, *, session_id=""): return None
    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""): return None
    def on_session_end(self, messages, *, session_id=""): return None
    def prefetch(self, query, *, session_id=""): return ""
    def system_prompt_block(self): return ""
    def shutdown(self):
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    def export(self):
        n = None
        if self._db is not None:
            n = self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return {"name": self.name, "best_for": self.best_for, "limits": self.limits,
                "search": list(self.search_modes), "write": False, "built_in": True,
                "messages": n, "db": str(self._path) if self._path else None}


def export_backend():
    return StateEpisodicBackend()
