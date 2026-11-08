"""Built-in "wiki" backend — wraps the wiki-memory provider + page SEARCH engine.

v0.1: route-all target, behavior like running wiki-memory directly (needs the
Hermes runtime: imports plugins.memory).
v0.3: `WikiIndex` (stdlib-only, NO runtime dependency) in-memory page index so
memrack_search('fts') hits wiki PAGES too — kind=page, target
wiki:<relpath>:L<n>, snippet (the retrieval tools.2); mtime-diff refresh pass on session
end (no per-write work).

Tests import WikiIndex directly — the wrapper's Hermes dependency stays quarantined.
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger("memrack.wiki")
_TOKEN = re.compile(r"[\w\-\.\:/]+", re.UNICODE)


def _utcnow_iso():
    """UTC, microseconds, Z-suffix — the ts norm of the merge (A-5)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class WikiIndex:
    """In-memory page index over a markdown tree. Pure stdlib, testable standalone."""

    def __init__(self, root, session_id=""):
        self._root = Path(str(root or (Path.home() / "wiki" / "main")))
        self._session_id = session_id
        self._index: dict = {}   # rel_path -> (mtime, text)
        self._lock = threading.RLock()   # REENTRANT: search() holds it and calls rescan()
        self._started = False
        self.rescan()   # index built once per start (session-start pass)
        self._started = True

    @property
    def root(self) -> Path:
        return self._root

    @property
    def pages(self) -> int:
        return len(self._index)

    def rescan(self) -> int:
        """Index-Refresh-Pass: new/changed pages in, deleted out. Returns change count."""
        if not self._root.is_dir():
            self._index = {}
            return 0
        fresh, changed = {}, 0
        for md in self._root.rglob("*.md"):
            rel = str(md.relative_to(self._root))
            try:
                mtime = md.stat().st_mtime
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            cached = self._index.get(rel)
            if cached is None or cached[0] != mtime:
                changed += 1
            fresh[rel] = (mtime, text)
        with self._lock:
            self._index = fresh
        if changed:
            _LOG.info("memrack wiki index: %d pages, %d changed", len(fresh), changed)
        return changed

    def search(self, query, mode, k, session_id=""):
        """Page hits per the retrieval tools.2: rank 1..k, AND-semantics over tokens, title boost."""
        if mode != "fts":
            raise ValueError("wiki engine carries mode 'fts' only (got %r)" % mode)
        with self._lock:
            self.rescan()
            snapshot = dict(self._index)
        tokens = [t.lower() for t in _TOKEN.findall(query or "") if len(t) > 1]
        if not tokens:
            return []
        now_iso = _utcnow_iso()
        scored = []
        for rel, (stored_mtime, text) in snapshot.items():
            try:
                ts = datetime.fromtimestamp(stored_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            except (TypeError, ValueError, OSError):
                ts = now_iso
            lines = text.splitlines()
            body = [l.lower() for l in lines]
            title = (lines[0] if lines else "").lower()
            score, first_line = 0.0, None
            for tok in tokens:
                if not any(tok in l for l in body):
                    score = -1.0   # AND semantics: one missing token kills the page
                    break
                score += 3.0 if tok in title else 0.0
                score += 1.0 * sum(1 for l in body if tok in l)
                if first_line is None:
                    first_line = next(i for i, l in enumerate(body, 1) if tok in l)
            if score > 0:
                scored.append({"backend": "wiki", "kind": "page", "rank": 0,
                               "target": "wiki:%s:%s" % (rel, ("L%d" % first_line) if first_line else ""),
                               "snippet": self._snippet(lines, first_line), "ts": ts,
                               "score": score})
        scored.sort(key=lambda h: (-h["score"], h["target"]))
        out = []
        for i, h in enumerate(scored[:k], 1):
            h["rank"] = i
            out.append({m: h[m] for m in ("backend", "kind", "rank", "target", "snippet", "ts")})
        return out

    @staticmethod
    def _snippet(lines, at):
        if not at or not lines:
            return ""
        lo, hi = max(0, at - 2), min(len(lines), at + 1)
        return " | ".join(l.strip()[:120] for l in lines[lo:hi] if l.strip())


class WikiBackend:
    name = "wiki"
    best_for = "structural knowledge / whole pages (rules, relationships); curation via the wiki notepad"
    limits = "no atomic write (pages are written by the agent with file tools)"
    search_modes = ("fts",)   # static; the index is runtime-built
    writeable = False
    write_types = ("explicit", "curated")   # the write-kind vocabulary (pages are agent-written)

    def __init__(self):
        self._prov = None
        self._session_id = ""
        self._engine: WikiIndex | None = None

    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        self._session_id = session_id or ""
        cfg = dict(provider_config or {})
        self._engine = WikiIndex(cfg.get("wiki_root"), session_id=self._session_id)
        try:
            from plugins.memory import load_memory_provider
        except Exception as exc:
            raise RuntimeError("wiki backend needs the Hermes runtime: %s" % exc) from exc
        prov = load_memory_provider("wiki-memory", register_skills=False)
        if prov is None:
            raise RuntimeError("wiki-memory provider not found (bundled or $HERMES_HOME/plugins)")
        prov.initialize(
            session_id=self._session_id,
            hermes_home=str(hermes_home) if hermes_home else None,
            **{k: v for k, v in kwargs.items() if k not in ("hermes_home", "provider_config")},
        )
        self._prov = prov

    # ── search engine delegation ──
    def search(self, query, mode, k, session_id=""):
        assert self._engine is not None, "not initialized"
        return self._engine.search(query, mode, k, session_id=session_id or self._session_id)

    def on_session_end(self, messages, *, session_id=""):
        if self._prov:
            self._prov.on_session_end(messages)
        if self._engine:
            self._engine.rescan()   # index-refresh pass

    def on_turn(self, user, assistant, *, session_id=""):
        if self._prov:
            self._prov.sync_turn(user, assistant, session_id=session_id)

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        if self._prov:
            self._prov.on_memory_write(action, target, content, metadata=metadata)

    def prefetch(self, query, *, session_id=""):
        if self._prov:
            return self._prov.prefetch(query, session_id=session_id or self._session_id) or ""
        return ""

    def system_prompt_block(self):
        return (self._prov.system_prompt_block() if self._prov else "") or ""

    def shutdown(self):
        if self._prov:
            self._prov.shutdown()

    def handle_tool_call(self, tool_name, args, **kwargs):
        """Pass-through: the wrapped provider's own tools stay reachable."""
        if self._prov is not None:
            fn = getattr(self._prov, "handle_tool_call", None)
            if fn is not None:
                return fn(tool_name, dict(args or {}), **kwargs)
        raise KeyError("wiki backend carries no tool %r" % tool_name)

    def export(self):
        return {
            "name": self.name,
            "best_for": self.best_for,
            "limits": self.limits,
            "search": list(self.search_modes),
            "write": self.writeable,
            "built_in": True,
            "provides": "wiki-memory wrapper + page search engine",
            "pages": self._engine.pages if self._engine else 0,
            "active": self._prov is not None,
        }

    @property
    def tool_schemas(self):
        try:
            return list(self._prov.get_tool_schemas() or []) if self._prov else []
        except Exception:
            return []


def export_backend():
    return WikiBackend()
