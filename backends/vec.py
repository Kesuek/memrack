"""Built-in "vec" backend: semantic recall over the fact ledger, opt-in.

Split law (2026-11-08): the tabular floor (base-db) carries facts/pointers/fts
ONLY; semantic recall lives here as its own backend with its OWN store
(``plugin-data/memrack/vec-index.db``). The fact ledger (``memrack.db`` in
base-db's store) is attached READ-ONLY for harvest and content joins — the
canonical ledger is never mutated by this backend, with one deliberate
exception: ``touch()`` bumps the shared heat counter on ledger rows (metadata,
not fact content).

Membership: off by default (``vec_in_rack`` toggle); when disabled the rack
has zero Ollama contact and zero cold start. ``mode='vec'`` without
membership = an honest error listing the available modes.
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

_LOG = logging.getLogger("memrack.backends.vec")

PLUGIN_NAME = "vec"

_EMBED_MODEL = "nomic-embed-text"
_OLLAMA_HOST = "http://127.0.0.1:11434"


def _utcnow_iso():
    """UTC-Z with microseconds — the cross-store chronology law."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "%06dZ" % (time.time() % 1 * 1e6)


class VecBackend:
    """Semantic recall backend over a read-only-attached fact ledger."""

    name = PLUGIN_NAME
    best_for = ("semantic recall over the fact ledger via a local embedding model "
                "(sqlite-vec); opt-in — the floor stays table-only for ms-fast fts")
    limits = ("requires a reachable local OpenAI-compatible embedder AND its membership "
              "toggle; cold start on the first query lives here; no ollama contact while "
              "membership is off; the ledger is attached read-only (heat bumps the "
              "shared counter only)")
    search_modes = ("vec",)
    writeable = False

    def __init__(self):
        self._store: sqlite3.Connection | None = None   # OWN store (facts_vec mirror)
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
        # URI on the main connection: the read-only ATTACH below needs it honored
        self._store = sqlite3.connect(
            "file:" + urllib.parse.quote(str(self._store_path), safe="/") + "?mode=rwc",
            check_same_thread=False, timeout=5.0, uri=True)
        self._vec_ok = False
        self._vec_gap = "embedder not probed yet"
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

    def _embed(self, texts):
        import urllib.request
        body = json.dumps({"model": self._model, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(self._host.rstrip("/") + "/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
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
                "CREATE TABLE IF NOT EXISTS idx_meta (key TEXT PRIMARY KEY, value TEXT);"
                "CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec2 USING vec0(embedding float[%d]);" % dim)
            return
        row = self._store.execute("SELECT vec_length(embedding) FROM facts_vec2 LIMIT 1").fetchone()
        if row and row[0] is not None and int(row[0]) != dim:
            self._vec_ok = False
            self._vec_gap = ("vec dimension mismatch: index expects %s, model %r produces %d"
                             % (row[0], self._model, dim))
            _LOG.warning("vec backend degraded: %s", self._vec_gap)

    # ── ledger attach (read-only; the canonical ledger is never mutated) ──
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

    # ── legacy migration (one-time, read-only attach of the old table) ──
    def _migrate_legacy(self):
        assert self._store is not None
        done = self._store.execute("SELECT value FROM idx_meta WHERE key='legacy_imported'").fetchone()
        if done:
            return
        rows = []
        try:
            attached = self._ledger()
            if attached is not None:
                exists = self._store.execute(
                    "SELECT 1 FROM ledger.sqlite_master WHERE name='facts_vec'"
                    " AND type='table'").fetchone()  # the LEGACY table lives in the ledger file
                if exists is not None:
                    rows = self._store.execute(
                        "SELECT rowid, embedding FROM ledger.facts_vec").fetchall()
        finally:
            self._detach()
        if not rows:
            with self._lock:
                self._store.execute(
                    "INSERT INTO idx_meta(key, value) VALUES('legacy_imported', 'empty-or-missing')")
                self._store.commit()
            return
        with self._lock:
            self._store.executemany(
                "INSERT OR IGNORE INTO facts_vec2(rowid, embedding) VALUES(?, ?)", rows)
            self._store.execute(
                "INSERT INTO idx_meta(key, value) VALUES('legacy_imported', ?)",
                ("imported=%d" % len(rows),))
            self._store.commit()
        _LOG.info("vec backend: legacy embeddings imported (%d)", len(rows))

    # ── harvest (incremental) + tombstones ──
    def _harvest(self):
        """Embed still-unindexed facts from the ledger, batched; prune dead ids.
        Returns (indexed, backlog) — the backlog surfaces honestly, never silently."""
        assert self._store is not None
        if self._ledger() is None:
            return 0, 0
        rows = dead = ()
        backlog = 0
        try:
            backlog = self._store.execute(
                "SELECT COUNT(*) FROM ledger.facts f LEFT JOIN facts_vec2 v ON v.rowid = f.id"
                " WHERE v.rowid IS NULL").fetchone()[0]
            rows = self._store.execute(
                "SELECT f.id, f.content FROM ledger.facts f"
                " LEFT JOIN facts_vec2 v ON v.rowid = f.id"
                " WHERE v.rowid IS NULL ORDER BY f.id LIMIT ?", (self._batch,)).fetchall()
            dead = [r[0] for r in self._store.execute(
                "SELECT v.rowid FROM facts_vec2 v LEFT JOIN ledger.facts f ON f.id = v.rowid"
                " WHERE f.id IS NULL").fetchall()]
        finally:
            self._detach()
        if dead:
            with self._lock:
                self._store.executemany("DELETE FROM facts_vec2 WHERE rowid = ?",
                                        [(d,) for d in dead])
                self._store.commit()
        if not rows:
            return 0, max(0, backlog)
        ids = [r[0] for r in rows]
        vecs = self._embed([r[1] for r in rows])
        if len(vecs) != len(rows):
            raise RuntimeError("embedding count mismatch: %d != %d" % (len(vecs), len(rows)))
        import sqlite_vec
        with self._lock:
            self._store.executemany(
                "INSERT OR IGNORE INTO facts_vec2(rowid, embedding) VALUES(?, ?)",
                [(rid, sqlite_vec.serialize_float32(v)) for rid, v in zip(ids, vecs)])
            self._store.commit()
        return len(rows), max(0, backlog - len(rows))

    # ── search ──
    def card_modes(self):
        return ("vec",) if self._vec_ok else ()

    def search(self, query, mode, k):
        if not self._vec_ok:
            raise ValueError("vec not available: %s" % self._vec_gap)
        if mode != "vec":
            raise ValueError("this engine carries mode %r not (modes: %s)" % (mode, ["vec"]))
        import sqlite_vec
        _, backlog = self._harvest()
        q_vecs = self._embed([query])
        if not q_vecs:
            return []
        qvec = sqlite_vec.serialize_float32(q_vecs[0])
        hits = []
        attached = self._ledger()
        try:
            if attached is None:
                return []
            rows = self._store.execute(
                "SELECT v.rowid, v.distance, f.content AS content, f.target AS target, "
                "f.ts AS ts, f.hits AS hits"
                " FROM facts_vec2 v LEFT JOIN ledger.facts f ON f.id = v.rowid"
                " WHERE v.embedding MATCH ? AND k = ?", (qvec, k)).fetchall()
        finally:
            self._detach()
        for i, (rid, dist, content, target, ts, heat) in enumerate(rows, 1):
            if content is None:
                continue  # tombstone race — the next harvest pass cleans it
            hits.append({"backend": self.name, "kind": "fact", "id": rid, "rank": i,
                         "content": content, "target": target or "base-db:%d" % rid,
                         "ts": ts, "hits": heat, "distance": round(float(dist), 4)})
        if backlog:
            for h in hits:
                h["_vec_backlog"] = backlog
        return hits

    # ── heat (the ONE shared counter; metadata-only ledger touch) ──
    def touch(self, ids):
        clean = []
        for tid in ids:
            try:
                clean.append(int(tid))
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        if not self._ledger_path or not self._ledger_path.exists():
            return
        conn = sqlite3.connect(str(self._ledger_path), timeout=5.0)
        try:
            for _attempt in range(5):
                try:
                    conn.executemany("UPDATE facts SET hits = hits + 1 WHERE id = ?",
                                     [(i,) for i in clean])
                    conn.commit()
                    break
                except sqlite3.OperationalError:
                    _LOG.warning("vec touch locked (attempt %d)", _attempt + 1)
                    time.sleep(0.2 * (_attempt + 1))
        finally:
            conn.close()

    # ── inert docked-contract stubs (the ledger keeps the facts) ──
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
        return {"name": self.name, "best_for": self.best_for, "limits": self.limits,
                "search": list(self.card_modes()), "write": self.writeable,
                "built_in": True, "vec": self._vec_ok, "model": self._model,
                "store": str(self._store_path) if self._store_path else None,
                "vec_gap": None if self._vec_ok else self._vec_gap}


def export_backend():
    return VecBackend()
