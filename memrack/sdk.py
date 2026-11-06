"""memrack SDK base class — the suite's lessons pre-built for backend authors.

This file imports NOTHING from the Hermes runtime: a backend built on it runs
(and tests) fully outside Hermes. Use is OPTIONAL — plain duck-typed modules
docking just as well.
"""

import re
import threading

fts5_hyphen_trap = 'hits like "Hit-me" are read by fts5 as column syntax -> always QUOTE tokens'


def fts_match_tokens(query, *, columns=None):
    """Build a safe fts5 MATCH fragment (the base_db lesson, reusable):
    tokens quoted individually (hyphen/colon safe), implicit AND over tokens;
    with `columns`, emits column-qualified OR-combination per column.
    Advanced passthrough: a query containing ':' rides as-is (deliberate)."""
    m = (query or "").strip().replace('"', "")
    if ":" in m:                      # the agent knows what he is doing — passthrough
        return m
    tokens = [t for t in re.split(r"\W+", m) if t]
    if not tokens:
        return ""
    quoted = ["%s" % ('"%s"' % t) for t in tokens]
    if columns:
        return " OR ".join('%s:(%s)' % (c, " ".join(quoted)) for c in columns)
    return " ".join(quoted)


class MemrackBackend:
    """Base class for memrack backends (T-015). Implement what you can:

    REQUIRED: name (attr), initialize(session_id=, hermes_home=, provider_config=, **kw)
    OPTIONAL HOOKS (no-op defaults here): on_turn, on_memory_write, on_session_end,
        prefetch -> "", system_prompt_block -> "", shutdown
    OPTIONAL ENGINE: search(query, mode, k) -> [hit], write(kind, content, tags=,
        session_id=) -> dict, touch(ids)
    CARD: best_for / limits / search_modes + card_modes() whenever availability is
        ENV-DEPENDENT (degrade honestly — the card must never promise a dead mode).

    Threading: the runtime may call hooks from worker threads — all shared state
    behind self._lock (REENTRANT: a locked method calling another locked one is
    the deadlock lesson: Lock vs RLock).
    """

    name = "set-your-backend-name"
    best_for = ""   # LEER = ungesetzt — die Conformance WARNT dann (keine Dummy-Texte)
    limits = ""
    search_modes: tuple = ()      # claim only what search() really implements
    writeable = False

    _REENTRANT = threading.RLock()  # per-instance in __init__; class attr is only the reminder

    def __init__(self):
        self._lock = threading.RLock()

    # ── required ──
    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        raise NotImplementedError("backend implementiert initialize() — store/connections, no network in probes")

    # ── optional hooks: safe no-ops (inherit and override what matters) ──
    def on_turn(self, user, assistant, *, session_id=""):
        return None

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        return None

    def on_session_end(self, messages, *, session_id=""):
        return None

    def prefetch(self, query, *, session_id=""):
        return ""

    def system_prompt_block(self):
        return ""

    def shutdown(self):
        pass

    # ── degrade/availability helper ──
    def card_modes(self):
        """Default: static modes. Override for env-dependent capability sets."""
        return tuple(self.search_modes)

    def export(self):
        return {"name": self.name, "best_for": self.best_for, "limits": self.limits,
                "search": list(self.card_modes()), "write": self.writeable}
