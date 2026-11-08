"""CatalogProxyBackend — wrap an installed, INACTIVE catalog MemoryProvider
(the Hermes ABC) as a docked memrack backend. Mirror, not rebuild.

Design-Contract (source of truth:
~/.hermes/projects/memrack/references/catalog-rack-adapter-design.md,
table "Proxy-Design: mirror-not-rebuild"): the provider NEVER talks to
the host directly — memrack mirrors the ABC and bends four channels:

  ① prefetch(query, session)   the provider text enters memrack's ONE
                               prefetch fusion (the T-031 provider-level
                               fan-out) — never a second, direct host
                               injection.
  ② system_prompt_block()      concat into memrack's block behind an
                               origin header line ("Concat in memracks
                               Block mit Herkunfts-Headerzeile").
  ③ get_tool_schemas           mirrored under the memrack namespace by
                               the 64-cap name chain (memrack/namechain.py)
                               — or deliberately NOT mirrored: a
                               per-provider decision, documented in the
                               card.
  pass-through for the rest    initialize/shutdown/export (the provider's
                               own store/deps/config stay with the
                               provider); on_turn -> sync_turn (their
                               ingestion); on_memory_write forwarded ONLY
                               when the write semantics are compatible,
                               otherwise the card says "retrieval only".

File-name note (deliberate deviation from the scaffold's rename plan): this
module KEEPS its leading underscore. discover_backends() loads every
NON-underscore *.py in backends/ as a code-first built-in — a renamed
``catalog_proxy.py`` would construct a provider-less proxy (empty name) on
EVERY host in EVERY session and die in the registry checks. The underscore
keeps this file a library (it carries no dockable provider); the per-provider
binding happens through docking-generated shims beside the draft card
(memrack/docking.write_draft_card), which resolve this file deterministically.
"""

import json
import sys
import threading

try:
    from memrack import namechain as _namechain
except ImportError:   # hermes-less / spec-loaded contexts: load the sibling
    import importlib.util
    import pathlib
    _nc_mod = sys.modules.get("memrack_adapter_namechain")
    if _nc_mod is None:
        _nc_spec = importlib.util.spec_from_file_location(
            "memrack_adapter_namechain",
            pathlib.Path(__file__).resolve().parent.parent / "memrack" / "namechain.py")
        if _nc_spec is None or _nc_spec.loader is None:
            raise ImportError("memrack name chain not loadable beside the adapter")
        _nc_mod = importlib.util.module_from_spec(_nc_spec)
        sys.modules["memrack_adapter_namechain"] = _nc_mod
        _nc_spec.loader.exec_module(_nc_mod)
    _namechain = _nc_mod

# The origin header of Umlenkung ② (wording is GREEN-phase decision space):
# line one of the fused prompt block names the provider — honest provenance.
_ORIGIN_HEADER = ("docked catalog provider: %s — the context below comes "
                  "from the provider mirror")


def _truth(value, default=False):
    """Read-side truth for card toggles (mirror of the provider core's
    `_truthy`): wizard/native booleans, "true"/"yes"/"on"/"1" strings —
    and an ABSENT decision key resolves to ``default`` (fail-closed)."""
    if isinstance(value, bool) or value is None:
        return bool(value) if value is not None else default
    return str(value or "").strip().lower() in ("true", "yes", "on", "1")


class CatalogProxyBackend:
    """A dockable backend card-shape around ONE catalog provider instance.

    Contract (the docking contract, memrack registry/conformance view):
    REQUIRED surface initialize/on_turn/on_memory_write/on_session_end/
    prefetch/system_prompt_block/shutdown/export; OPTIONAL engine surface
    (search) never claims a memrack mode generically (see search()).

    Card placeholders (design: "best_for/limits bleiben menschlich
    autorisiert"): the code claims NOTHING — best_for/limits stay empty
    until the dock review fills the card file, search_modes claim nothing
    by themselves (the probe derives the honest modes). The conformance
    warns about empty card text honestly instead of shipping dummy copy.
    """

    name = ""           # card identity = wrapped provider name; set in __init__
    best_for = ""       # PLACEHOLDER by design — human-authored at dock review
    limits = ""         # PLACEHOLDER by design
    search_modes = ()   # claims nothing on its own; card/probe own the truth
    writeable = False   # write-forwarding is a per-provider DECISION, not a claim

    def __init__(self, provider=None, *, loader=None, mirror_tools=None,
                 forward_writes=None, query_capable=None):
        """Bind ONE wrapped provider + the per-provider decisions.

        ``provider``: a live provider instance (test doubles, probe wiring);
        ``loader``: callable () -> provider instance — the isolated load seam
        (hermes-plugins doctor pattern; resolved at initialize, fresh
        instance per load, ABC multiplexing contract).

        The decision seams (each documented in the card; None = UNDECIDED =
        fail-closed off — design: "Entscheidung je Provider, in der Card
        dokumentiert", mirroring/writing without a recorded decision is not
        mirroring/writing):
        - ``mirror_tools``: Umlenkung ③ on/off (True: mirror the provider's
          tools under the memrack namespace via the name chain).
        - ``forward_writes``: the on_memory_write Forward decision (design
          row: only compatible write semantics; else "retrieval only").
        - ``query_capable``: the probe outcome of
          prefetch-only vs. query-capable (Sampling-Bias-Law: the probe
          decides per provider, never a prior).
        """
        self._lock = threading.RLock()
        self._provider = provider
        self._loader = loader
        self._dec_mirror = mirror_tools
        self._dec_writes = forward_writes
        self._dec_query = query_capable
        self._mirror_orig = {}       # {original tool name: effective name}
        self._mirrors = {}           # {effective name: original tool name}
        self._mirror_error = ""
        self._docked = False         # True for docking-generated bindings
        self.manifest = None
        self.manifest_file = None
        if provider is not None:
            self.name = str(getattr(provider, "name", "") or "")

    # ── per-provider decisions: recorded decision data, never inferred ──

    def _mirror_enabled(self):
        """Umlenkung ③ on/off: explicit constructor decision > the card's
        recorded ``mirror_decision`` > a recorded mirror_tools mapping >
        undecided = fail-closed OFF."""
        if self._dec_mirror is not None:
            return self._dec_mirror
        man = getattr(self, "manifest", None)
        if isinstance(man, dict):
            decision = man.get("mirror_decision")
            if decision is not None and str(decision).strip().lower() in ("mirror", "off"):
                return str(decision).strip().lower() == "mirror"
            if isinstance(man.get("mirror_tools"), dict):
                return bool(man["mirror_tools"])   # a recorded mapping IS the decision
        return False

    def _writes_enabled(self):
        """The on_memory_write Forward decision: constructor > the card's
        ``forward_writes`` key > undecided = retrieval-only (fail-closed:
        an incompatible provider NEVER receives mirror writes)."""
        if self._dec_writes is not None:
            return self._dec_writes
        man = getattr(self, "manifest", None)
        if isinstance(man, dict) and "forward_writes" in man:
            return _truth(man.get("forward_writes"), default=False)
        return False

    def _available(self):
        """Provider availability (the card's honesty gate): claims nothing
        while the provider cannot serve (dead modes never promised)."""
        provider = self._provider
        if provider is None:
            return False
        gate = getattr(provider, "is_available", None)
        if not callable(gate):
            return False
        try:
            return bool(gate())
        except Exception:
            return False     # a failing gate degrades the card, never lies

    def _provider_ready(self):
        return self._provider is not None

    # ── mirror map (deterministic; rebuilt-free after first resolve) ──

    def _mirror_map_on(self, provider):
        """{original: effective} for the provider's CURRENT tool list — the
        live truth, computed from the deterministic name chain. A set whose
        effective names collide raises MirrorNameError (fail-closed law)."""
        schemas = []
        if callable(getattr(provider, "get_tool_schemas", None)):
            schemas = list(provider.get_tool_schemas() or [])
        tools = []
        for schema in schemas:
            tool = _namechain.extract_tool_name(schema)
            if tool:
                tools.append(str(tool))
        mapping = _namechain.mirror_tool_names(self.name, tools)
        # per-OCCURRENCE names: the same tool registered twice is itself the
        # collision case — two registrations fight over one effective name
        # (a dict would silently collapse that; the law forbids it).
        _namechain.ensure_unique(
            [mapping[tool] for tool in tools],
            origin="provider %r tool mirror" % self.name)
        return mapping

    def _refresh_mirror_maps(self):
        rebuild = None
        with self._lock:
            if not self._mirror_enabled():
                self._mirror_orig, self._mirrors = {}, {}
                return {}
            rebuild = self._mirror_map_on(self._provider)
            self._mirror_orig = dict(rebuild)
            self._mirrors = {eff: orig for orig, eff in rebuild.items()}
            self._mirror_error = ""
        return rebuild

    # ── lifecycle: pass-through (the provider keeps its own bindings) ──
    def initialize(self, session_id="", hermes_home=None, provider_config=None, **kwargs):
        """Pass-through ABC binding (design row "initialize / shutdown /
        export: Pass-through — eigener Store/Deps/Config verbleibt beim
        Provider"). The proxy binds the provider instance (or resolves it
        via the loader once — fresh instance per load, the ABC
        multiplexing contract) and hands initialize through unchanged;
        no proxy-owned store, no credential guessing (fail-closed)."""
        provider = self._provider
        if provider is None and callable(self._loader):
            provider = self._loader()
        if provider is None and self._docked:
            provider = self._load_docked_provider()
        if provider is None:
            raise RuntimeError(
                "catalog proxy %r has no provider bound (fail-closed): pass "
                "provider= or loader= — the proxy never invents a provider"
                % (self.name or "?"))
        self._provider = provider
        if not self.name:
            self.name = str(getattr(provider, "name", "") or "")
        with self._lock:
            if self._mirror_enabled():
                try:
                    self._refresh_mirror_maps()
                except _namechain.MirrorNameError as exc:
                    self._mirror_error = str(exc)
                    self._mirror_orig, self._mirrors = {}, {}
                    raise RuntimeError(
                        "catalog dock %r refused (fail-closed mirror registration): %s"
                        % (self.name, exc)) from exc
        # the HOST contract binds providers with initialize(session_id,
        # hermes_home=...): the docked provider sees the SAME binding home the
        # rack got — its own store stays inside the profile home instead of
        # silently falling back to its own default home.
        provider.initialize(session_id=session_id, hermes_home=hermes_home,
                            **kwargs)

    def _load_docked_provider(self):
        """The real-path loader (docking-generated bindings): resolve the
        provider through the SAME isolation the hermes-plugins doctor uses —
        register_skills=False keeps the inspection free of registry side
        effects; a None load means the provider is broken/uninstalled and
        NOTHING is invented for it."""
        from plugins import memory as _pm   # lazy: hermes-internal, never at import
        provider = _pm.load_memory_provider(self.name, register_skills=False)
        if provider is None:
            raise RuntimeError("docked memory provider %r is not loadable "
                               "(installed copy broken or missing)" % self.name)
        return provider

    def shutdown(self):
        """Pass-through; a broken provider shutdown is logged, never fatal
        (the rack's own shutdown discipline)."""
        provider = self._provider
        if provider is None:
            return
        fn = getattr(provider, "shutdown", None)
        if not callable(fn):
            return
        try:
            fn()
        except Exception:
            pass    # logged by the caller (rack shutdown logs per-backend failures)

    def export(self):
        """Card-shaped backend export (the docking contract export shape:
        {name, best_for, limits, search, write} plus the docked-provider
        evidence fields the status card shows: provider origin + draft/
        reviewed state)."""
        out = {"name": self.name, "best_for": self.best_for, "limits": self.limits,
               "search": list(self.card_modes()), "write": self.writeable}
        man = getattr(self, "manifest", None)
        if isinstance(man, dict):
            out["docked"] = True
            if man.get("draft"):
                out["draft"] = True
            evidence = man.get("evidence") or {}
            if evidence.get("provider_version"):
                out["provider_version"] = evidence["provider_version"]
            if evidence.get("probed_at"):
                out["probed_at"] = evidence["probed_at"]
        return out

    # ── Umlenkung 1: prefetch fusion (never a direct host injection) ──
    def prefetch(self, query, *, session_id=""):
        """Kanal ①: return the provider's OWN prefetch text — WITHOUT
        touching the host. The result does NOT go to the host: memrack
        catches it and fuses it into ONE own prefetch block (the fan-out
        join; cache-friendly single injection rail)."""
        provider = self._provider
        if provider is None:
            return ""
        fn = getattr(provider, "prefetch", None)
        if not callable(fn):
            return ""
        return fn(query, session_id=session_id) or ""

    def queue_prefetch(self, query, *, session_id=""):
        """Pass-through background-recall queueing (the memrack fan-out
        reaches the rack's queue_prefetch for the ACTIVE backend; the proxy
        serves the provider's queue hook when it carries one)."""
        provider = self._provider
        if provider is None:
            return None
        fn = getattr(provider, "queue_prefetch", None)
        if not callable(fn):
            return None
        return fn(query, session_id=session_id)

    def recall_status(self):
        """Pass-through of the provider's last-recall indicator (None when
        the provider does not report one); the rack owns the indicator
        policy, the proxy only mirrors what is honestly there."""
        provider = self._provider
        if provider is None:
            return None
        fn = getattr(provider, "recall_status", None)
        if not callable(fn):
            return None
        return fn()

    # ── Umlenkung 2: system prompt concat with origin line ──
    def system_prompt_block(self):
        """Kanal ②: the provider's STATIC block enters memrack's block
        CONCATENATED behind an origin line naming the provider. An empty
        provider block contributes NOTHING (the non-empty-parts join law —
        no phantom origin headers; a silent provider stays silent)."""
        provider = self._provider
        if provider is None:
            return ""
        fn = getattr(provider, "system_prompt_block", None)
        if not callable(fn):
            return ""
        block = str(fn() or "").strip()
        if not block:
            return ""
        return "\n".join([_ORIGIN_HEADER % self.name, "", block])

    # ── Umlenkung 3: tool mirror under the memrack namespace ──
    def tool_schemas(self):
        """Kanal ③ (hard): the provider's registered tool schemas, mirrored
        under the 64-cap name chain; a provider whose decision says NO
        mirroring carries no mirrored schemas ([] = the mirror-off state,
        never an error). A colliding mirror set turns OFF (error recorded,
        honest silence — the fail-closed law: no mangled names, no winner
        picks); initialize() already refuses such a dock."""
        with self._lock:
            if not self._mirror_enabled():
                return []
            try:
                self._refresh_mirror_maps()
            except _namechain.MirrorNameError as exc:
                self._mirror_error = str(exc)
                return []
            if not self._mirrors:
                return []
        provider = self._provider
        if provider is None:
            return []
        fn = getattr(provider, "get_tool_schemas", None)
        if not callable(fn):
            return []
        schemas = []
        for schema in list(fn() or []):
            tool = _namechain.extract_tool_name(schema)
            if not tool:
                continue
            effective = self._mirror_orig.get(tool)
            if not effective:
                continue      # a schema that drifted off the chain maps NOWHERE
            schemas.append(_namechain.with_tool_name(schema, effective))
        return schemas

    def handle_mirrored_call(self, name, args, **kwargs):
        """Dispatch ONE mirrored call: the HOST-side mirrored name maps back
        to the provider's ORIGINAL tool name and hands provider.handle_
        tool_call the original name (the provider never sees the memrack
        namespace). Unknown mirrored names fail closed (KeyError, never a
        guessed tool). The mapping comes from the deterministic name
        chain, never from registration order."""
        self._refresh_mirror_maps()
        original = self._mirrors.get(str(name or ""))
        if original is None:
            raise KeyError(
                "tool %r is not a registered mirrored name of %r (fail-closed: "
                "nothing dispatched)" % (str(name or ""), self.name))
        provider = self._provider
        if provider is None:
            raise KeyError("catalog dock %r has no provider bound" % self.name)
        fn = getattr(provider, "handle_tool_call", None)
        if not callable(fn):
            raise KeyError("provider %r does not answer tool calls" % self.name)
        return fn(original, dict(args or {}), **kwargs)

    # ── pass-through ingestion + capture hooks (rest of the ABC) ──
    def on_turn(self, user, assistant, *, session_id=""):
        """Pass-through to the provider's sync_turn (design row
        "on_turn_complete / sync_turn: Pass-through — deren Ingestion")."""
        provider = self._provider
        if provider is None:
            return None
        fn = getattr(provider, "sync_turn", None)
        if not callable(fn):
            return None
        return fn(user, assistant, session_id=session_id)

    def on_memory_write(self, action, target, content, metadata=None, *, session_id=""):
        """Forward — per provider DECISION (design row: "nur wenn Write-
        Semantik kompatibel; sonst Card sagt 'nur Retrieval'"). The dock
        decision rides the card (forward_writes); an incompatible provider
        NEVER receives mirror writes (the host's builtin-store writes stay
        builtin-owned; the receipt None = the memrack no-op)."""
        if not self._writes_enabled():
            return None
        provider = self._provider
        if provider is None:
            return None
        fn = getattr(provider, "on_memory_write", None)
        if not callable(fn):
            return None     # the decision says forward, the provider cannot — honest no-op
        return fn(action, target, content, metadata=metadata)

    def on_session_end(self, messages, *, session_id=""):
        """Pass-through end-of-session capture (rest = pass-through; the
        memrack fan-out reaches this for the capture)."""
        provider = self._provider
        if provider is None:
            return None
        fn = getattr(provider, "on_session_end", None)
        if not callable(fn):
            return None
        return fn(messages)

    def on_pre_compress(self, messages, **kwargs):
        """Pass-through pre-compression capture (rest pass-through)."""
        provider = self._provider
        if provider is None:
            return None
        fn = getattr(provider, "on_pre_compress", None)
        if not callable(fn):
            return None
        return fn(messages)

    def on_session_switch(self, new_session_id=None, **kwargs):
        """Pass-through session rebinding (rest pass-through)."""
        provider = self._provider
        if provider is None:
            return None
        fn = getattr(provider, "on_session_switch", None)
        if not callable(fn):
            return None
        return fn(new_session_id, **kwargs)

    def on_delegation(self, task=None, result=None, **kwargs):
        """Pass-through delegation observation (rest pass-through)."""
        provider = self._provider
        if provider is None:
            return None
        fn = getattr(provider, "on_delegation", None)
        if not callable(fn):
            return None
        return fn(task, result, **kwargs)

    # ── retrieval engine (the card-recorded query bridge) ──
    def search(self, query, mode, k, session_id=""):
        """Fan-out hit source through the CARD-RECORDED query bridge.

        A defined method is NOT a claim — the CARD is (the honest-claims law).
        The per-provider bridge is DELIBERATE WORK: the card records how THIS
        provider is queried (``query_bridge``) — the adapter executes a
        recorded recipe and never guesses a provider's query signature
        (Sampling-Bias-Law: the probe records the retrieval evidence; the
        card authors the claim; the review confirms it). Without a recorded
        recipe the mode claim stays an honest error, exactly like before.

        Recipe (card data, all keys optional beyond via/tool):

            query_bridge:
              via: tool            # the execution rail
              tool: recall         # the provider's ORIGINAL tool name
              query_arg: query     # args key carrying the query (default)
              args: {...}          # additional static args (optional)
              results_key: results # list key in the tool's JSON answer
              kind: fact           # the hit kind label in the fused pool

        Failure discipline (honest-degrade law): a provider tool answering an
        error payload raises (the fan-out shows the engine error, never a
        silent zero); the provider's own abstain shape (empty results) is an
        honest zero.
        """
        man = getattr(self, "manifest", None)
        claimed = tuple((man or {}).get("search") or ()) if \
            isinstance(man, dict) else self.search_modes
        if not claimed:
            raise ValueError(
                "backend %r carries no memrack search modes (retrieval runs over "
                "the prefetch rail and the mirrored tools — the card says so)"
                % self.name)
        if mode not in claimed:
            raise ValueError(
                "backend %r does not carry mode %r (card claims %r)" %
                (self.name, mode, list(claimed)))
        if not self._available():
            raise ValueError(
                "backend %r is not available right now (card modes degrade, "
                "never lie)" % self.name)
        recipe = self._bridge_recipe()
        if recipe is None:
            raise ValueError(
                "backend %r claims search mode(s) %r but the card records no "
                "query bridge (fail-closed: the adapter never guesses a "
                "provider's query signature)" % (self.name, list(claimed)))
        provider = self._provider
        if provider is None:
            raise ValueError("backend %r has no provider bound" % self.name)
        fn = getattr(provider, "handle_tool_call", None)
        if not callable(fn):
            raise ValueError(
                "backend %r's provider does not answer tool calls" % self.name)
        tool = str(recipe.get("tool") or "")
        if not tool:
            raise ValueError(
                "backend %r's query_bridge records no tool" % self.name)
        args = {str(recipe.get("query_arg") or "query"): str(query)}
        args.update(dict(recipe.get("args") or {}))
        raw = fn(tool, args, session_id=session_id)
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise ValueError(
                    "backend %r's bridge tool %r answered no JSON: %s"
                    % (self.name, tool, exc)) from exc
        else:
            data = raw
        if isinstance(data, dict) and data.get("error"):
            raise ValueError(
                "backend %r's bridge tool %r refused: %s" %
                (self.name, tool, data.get("error")))
        items = data.get(str(recipe.get("results_key") or "results")) \
            if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
        kind = str(recipe.get("kind") or "fact")
        hits = []
        for i, item in enumerate(items[:max(1, int(k))]):
            if isinstance(item, str):
                content, extra = item, {}
            elif isinstance(item, dict):
                content = str(item.get(str(recipe.get("content_key") or "content"))
                              or item.get("content") or "")
                extra = {key: item.get(key)
                         for key in ("id", "target", "ts")
                         if item.get(key) is not None}
            else:
                continue
            if not content:
                continue
            hit = {"backend": self.name, "kind": kind, "rank": i + 1,
                   "content": content}
            hit.update(extra)
            hits.append(hit)
        return hits

    def _bridge_recipe(self):
        """The card's recorded query_bridge recipe, or None (fail-closed)."""
        man = getattr(self, "manifest", None)
        if not isinstance(man, dict):
            return None
        recipe = man.get("query_bridge")
        if not isinstance(recipe, dict) or not recipe:
            return None
        via = str(recipe.get("via") or "tool").strip().lower()
        if via != "tool":      # a recorded rail the executor cannot run = no bridge
            return None
        return recipe

    def card_modes(self):
        """Availability-driven modes: the card claims the manifest's derived
        modes ONLY while the provider reports itself available (the card
        honesty law — dead modes never promised; the dock degrades, never
        invents)."""
        if not self._available():
            return ()
        man = getattr(self, "manifest", None)
        if isinstance(man, dict) and man.get("search"):
            return tuple(man["search"])
        return tuple(self.search_modes)


def from_dock(provider_name):
    """The docking-generated binding entry (module-level twin of the shim's
    dispatch — kept beside the class so the shim template stays tiny).
    """
    backend = CatalogProxyBackend()
    backend.name = str(provider_name or "")
    backend._docked = True
    return backend


def export_backend():
    """Module-level contract note: this adapter file is a LIBRARY, not a
    dockable backend (the leading `_` keeps it out of every rack).

    The REAL per-provider binding = the docking-generated shim beside the
    draft card: backends.d/<provider>.py exposing export_backend() that
    calls CatalogProxyBackend.from_dock('<provider>'). The shared file is
    resolved from the shim (its own plugin root first, then the recorded
    generation-time path) — one shared adapter, many provider cards.
    """
    raise NotImplementedError(
        "the catalog adapter module itself is not a dockable backend — "
        "import CatalogProxyBackend/from_dock or generate a per-provider "
        "shim (memrack/docking.write_draft_card); design contract "
        "catalog-rack-adapter-design.md (mirror-not-rebuild)")
