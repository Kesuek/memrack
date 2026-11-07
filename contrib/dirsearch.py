"""dirsearch — generic read-only FTS backend over a configured
config directory (markdown/text files).

Single lines are the entries (target = `dirsearch:<relpath>:L<n>`),
every hit object carries a short snippet. This backend writes
nothing: writeable=False, no heat counter, no index —
each search scans the files live with simple term scoring.

Without provider_config['root'] the backend is honestly useless:
card_modes() == () — nothing promised that it does not keep.
Docking manifest beside it: dirsearch.yaml (name, module, best_for, limits, search).
"""

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

# SDK: OPTIONAL. If importable we inherit the lessons; otherwise plain duck typing.
try:
    from memrack.sdk import MemrackBackend, fts_match_tokens
except ImportError:
    class MemrackBackend:            # duck typing: ALL contract hooks as no-ops
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


class DirsearchBackend(MemrackBackend if MemrackBackend is not object else object):
    name = "dirsearch"                      # UNIQUE in the rack
    best_for = ("re-finding single lines across markdown/text file collections "
                "(config dirs, note sets) via full-text term search.")
    limits = ("read-only (no write, no heat counter); no vec, no index — "
              "scan per query; needs provider_config['root'], else useless.")
    search_modes = ("fts",)                 # carried only once a root is configured (see card_modes)
    writeable = False

    TEXT_SUFFIXES = (".md", ".markdown", ".text", ".txt", ".rst")
    SKIP_DIRS = ("node_modules", "__pycache__", ".venv", "venv")
    MAX_FILE_BYTES = 1 << 20                # 1 MiB per file — config files are small
    MAX_FILES = 4000                        # scan budget per query
    MAX_LINES_PER_FILE = 3                  # no file floods the hit list
    SNIPPET_CHARS = 200

    def __init__(self):
        if hasattr(MemrackBackend, "initialize"):
            super().__init__()              # SDK sugar (its RLock discipline)
        self._lock = threading.RLock()
        self._root = None                   # Path — or None (no root -> honestly empty)

    # -- lifecycle ---------------------------------------------------------

    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        """Root comes from provider_config['root']; without one card_modes() stays empty.

        No storage of its own: this backend writes nothing, everything lives with
        the caller (the configured config directory)."""
        with self._lock:
            cfg = provider_config or {}
            root = str(cfg.get("root") or "").strip()
            self._root = None
            if root:
                p = Path(root).expanduser().resolve()
                if p.is_dir():
                    self._root = p          # dead directory -> useless, do not lie
        return None

    def is_available(self):
        """Config dependency, no network access (guide law 5)."""
        return self._root is not None and self._root.is_dir()

    def card_modes(self):
        """Honest card: without a root nothing is promised."""
        return ("fts",) if self.is_available() else ()

    def get_config_schema(self):
        """What `hermes memory setup` reads (guide law 5)."""
        return {
            "root": {
                "type": "path", "required": True, "default": "",
                "description": "Directory of markdown/text files that gets searched.",
            },
        }

    # -- search engine -----------------------------------------------------

    def search(self, query, mode, k, session_id=""):
        if not self.is_available():
            raise ValueError("dirsearch: no root configured (provider_config['root'])")
        if mode != "fts":
            raise ValueError("dirsearch carries fts only")
        with self._lock:
            tokens = _lower_tokens(query)
            candidates = []
            for path, rel in self._text_files():
                candidates.extend(self._match_file(tokens, path, rel))
            # Global: best score first, ties by path, then line number.
            candidates.sort(key=lambda h: (-h["_score"], h["target"]))
            out = []
            for i, h in enumerate(candidates[:k], 1):
                out.append({
                    "backend": self.name,
                    "kind": "pointer",   # read-only + reference hits: no heat obligation (kind vocab)
                    "rank": i,
                    "target": h["target"],
                    "snippet": h["snippet"],
                    "ts": h["ts"],
                })
            return out

    def _text_files(self):
        """Deterministic walk of the root; text/markdown files only."""
        if self._root is None:
            return
        seen = 0
        for dirpath, dirnames, filenames in os.walk(self._root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d not in self.SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                p = Path(dirpath) / fn
                if p.suffix.lower() not in self.TEXT_SUFFIXES:
                    continue
                if seen >= self.MAX_FILES:
                    return
                seen += 1
                try:
                    rel = p.relative_to(self._root).as_posix()
                except ValueError:
                    continue
                yield p, rel

    def _match_file(self, tokens, path, rel):
        if not tokens:                       # empty query = honestly no hits
            return []
        try:
            if path.stat().st_size == 0 or path.stat().st_size > self.MAX_FILE_BYTES:
                return []
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        ts = self._ts(path)
        scored = []
        for lineno, line in enumerate(text.splitlines(), 1):
            score = self._score_line(tokens, line)
            if score <= 0:
                continue
            snippet = line.strip()
            if len(snippet) > self.SNIPPET_CHARS:
                snippet = snippet[:self.SNIPPET_CHARS - 1] + "\u2026"
            scored.append((score, lineno, snippet))
        # per file only the best lines, ties -> earlier line wins
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [{
            "target": "dirsearch:%s:L%d" % (rel, lineno),
            "snippet": snippet,
            "ts": ts,
            "_score": score,
        } for score, lineno, snippet in scored[:self.MAX_LINES_PER_FILE]]

    @staticmethod
    def _score_line(tokens, line):
        low = line.lower()
        score = 0
        for tok in tokens:
            if not tok or tok not in low:
                continue
            score += low.count(tok)                       # substring hits
            score += len(re.findall(r"(?<![a-z0-9_\u00e4\u00f6\u00fc\u00df])%s(?![a-z0-9_\u00e4\u00f6\u00fc\u00df])"
                                    % re.escape(tok), low))  # word-boundary boost
        return score

    def _ts(self, path):
        """UTC-Z with microseconds (guide law 1) — mtime as the honest state."""
        try:
            dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            dt = datetime.now(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # -- introspection -----------------------------------------------------

    def export(self):
        n = 0
        if self.is_available():
            n = sum(1 for _ in self._text_files())
        return {
            "name": self.name,
            "best_for": self.best_for,
            "limits": self.limits,
            "search": list(self.card_modes()),
            "write": self.writeable,
            "root": str(self._root) if self._root else "",
            "files": n,
        }


def _lower_tokens(query):
    return [t.lower() for t in fts_match_tokens(query).replace('"', "").split() if t]


def export_backend():
    """REQUIRED: the docking contract — registry/conformance expect exactly this."""
    return DirsearchBackend()
