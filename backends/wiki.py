"""Built-in "wiki" backend — wraps the wiki-memory provider.

v0.1 target of route-all: memrack + this backend behaves like running
wiki-memory directly. Needs the Hermes runtime (imports plugins.memory).
"""


class WikiBackend:
    name = "wiki"
    write_types = ("explicit", "episodic", "semantic", "curation")  # v0.1: all

    def __init__(self):
        self._prov = None
        self._session_id = ""

    def initialize(self, session_id="", hermes_home=None, **kwargs):
        self._session_id = session_id or ""
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
            **{k: v for k, v in kwargs.items() if k != "hermes_home"},
        )
        self._prov = prov

    def on_turn(self, user, assistant, *, session_id=""):
        if self._prov:
            self._prov.sync_turn(user, assistant, session_id=session_id)

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        if self._prov:
            self._prov.on_memory_write(action, target, content, metadata=metadata)

    def on_session_end(self, messages, *, session_id=""):
        if self._prov:
            self._prov.on_session_end(messages)

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
            "write_types": list(self.write_types),
            "provides": "wiki-memory wrapper (built-in)",
            "active": self._prov is not None,
        }

    # additive surface: expose the wrapped provider's own tools untouched
    @property
    def tool_schemas(self):
        try:
            return list(self._prov.get_tool_schemas() or []) if self._prov else []
        except Exception:
            return []


def export_backend():
    return WikiBackend()
