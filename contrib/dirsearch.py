"""dirsearch — generisches read-only FTS-Backend ueber ein konfiguriertes
Konfig-Verzeichnis (Markdown/Textdateien).

Einzelne Zeilen sind die Eintraege (target = `dirsearch:<relpath>:L<n>`),
jedes Treffer-Objekt traegt ein kurzes Snippet. Schreiben tut dieses
Backend nichts: writeable=False, kein Heat-Counter, kein Index —
jede Suche scannt die Dateien live mit einfacher Term-Bewertung.

Ohne provider_config['root'] ist das Backend ehrlich unbrauchbar:
card_modes() == () — nichts versprochen, was es nicht haelt.
Docking-Manifest daneben: dirsearch.yaml (name, module, best_for, limits, search).
"""

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

# SDK: OPTIONAL. Wenn importierbar, erben wir die Lektionen; sonst reiner Duck-Typ.
try:
    from memrack.sdk import MemrackBackend, fts_match_tokens
except ImportError:
    class MemrackBackend:            # duck-full: ALLE Contract-Hooks als No-Ops
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
    name = "dirsearch"                      # UNIQUE im Rack
    best_for = ("re-finding single lines across markdown/text file collections "
                "(config dirs, note sets) via full-text term search.")
    limits = ("read-only (no write, no heat counter); no vec, no index — "
              "scan per query; needs provider_config['root'], else useless.")
    search_modes = ("fts",)                 # gefuehrt nur, wenn Root konfiguriert (siehe card_modes)
    writeable = False

    TEXT_SUFFIXES = (".md", ".markdown", ".text", ".txt", ".rst")
    SKIP_DIRS = ("node_modules", "__pycache__", ".venv", "venv")
    MAX_FILE_BYTES = 1 << 20                # 1 MiB pro Datei — Konfig-Dateien sind klein
    MAX_FILES = 4000                        # Scan-Budget pro Query
    MAX_LINES_PER_FILE = 3                  # keine Datei flutet die Trefferliste
    SNIPPET_CHARS = 200

    def __init__(self):
        if hasattr(MemrackBackend, "initialize"):
            super().__init__()              # SDK-Zucker (seine RLock-Disziplin)
        self._lock = threading.RLock()
        self._root = None                   # Path — oder None (kein Root -> ehrlich leer)

    # -- lifecycle ---------------------------------------------------------

    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        """Root kommt aus provider_config['root']; ohne Root bleibt card_modes() leer.

        Kein eigener Storage: dieses Backend schreibt nichts, alles liegt beim
        Aufrufer (das konfigurierte Konfig-Verzeichnis)."""
        with self._lock:
            cfg = provider_config or {}
            root = str(cfg.get("root") or "").strip()
            self._root = None
            if root:
                p = Path(root).expanduser().resolve()
                if p.is_dir():
                    self._root = p          # totes Verzeichnis -> unbrauchbar, nicht luegen
        return None

    def is_available(self):
        """Config-Abhaengigkeit, kein Netz-Zugriff (Guide Gesetz 5)."""
        return self._root is not None and self._root.is_dir()

    def card_modes(self):
        """Ehrliche Card: ohne Root wird nichts versprochen."""
        return ("fts",) if self.is_available() else ()

    def get_config_schema(self):
        """Was `hermes memory setup` liest (Guide Gesetz 5)."""
        return {
            "root": {
                "type": "path", "required": True, "default": "",
                "description": "Verzeichnis mit Markdown-/Textdateien, das durchsucht wird.",
            },
        }

    # -- Suchengine --------------------------------------------------------

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
            # Global: besten Score zuerst, bei Gleichstand Pfad, dann Zeilennummer.
            candidates.sort(key=lambda h: (-h["_score"], h["target"]))
            out = []
            for i, h in enumerate(candidates[:k], 1):
                out.append({
                    "backend": self.name,
                    "kind": "pointer",   # read-only + reference-hits: kein Heat-obligate (kind-vocab)
                    "rank": i,
                    "target": h["target"],
                    "snippet": h["snippet"],
                    "ts": h["ts"],
                })
            return out

    def _text_files(self):
        """deterministischer Walk durch den Root; nur Text-/Markdown-Dateien."""
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
        if not tokens:                       # leere Query = ehrlich kein Treffer
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
        # pro Datei nur die besten Zeilen, Gleichstand -> fruehere Zeile gewinnt
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
            score += low.count(tok)                       # Substring-Treffer
            score += len(re.findall(r"(?<![a-z0-9_\u00e4\u00f6\u00fc\u00df])%s(?![a-z0-9_\u00e4\u00f6\u00fc\u00df])"
                                    % re.escape(tok), low))  # Wortgrenzen-Boost
        return score

    def _ts(self, path):
        """UTC-Z mit Mikrosekunden (Guide Gesetz 1) — mtime als ehrlicher Stand."""
        try:
            dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            dt = datetime.now(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # -- Introspektion -----------------------------------------------------

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
    """REQUIRED: der Docking-Contract — Registry/Conformance brauchen genau das."""
    return DirsearchBackend()
