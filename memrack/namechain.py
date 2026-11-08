"""Mirrored tool names — the 64-cap name chain (catalog-rack adapter).

Design-Contract (source of truth:
~/.hermes/projects/memrack/references/catalog-rack-adapter-design.md,
section "Namensgesetz für gespiegelte Tools (64-Cap-Kette)"):

    memrack__<provider>__<tool>
      -> at >64 chars: mr__<provider>__<tool>
      -> still >64:    mr__<p>__<t>__<hash4>   (deterministic digest from
                                               provider+tool — NEVER the
                                               registration order: the alias
                                               must survive restarts or the
                                               prompt cache breaks)

Laws the tests pin (tests/test_catalog_adapter.py):
- deterministic + restart-stable: the same (provider, tool) pair produces
  the same effective name on every call, in every session;
- bounded: an effective name is never longer than NAME_CAP chars;
- collision fail-closed: a mirror set whose members would share ONE
  effective name aborts with MirrorNameError (design: "Kollisionen ->
  Conformance bricht mit Fehler ab (fail-closed)" — never a silent
  de-duplication or a winner pick);
- effective names always sit in the memrack_status card (a docking-machine
  duty, see memrack/docking.py).

Pure stdlib only: importable hermes-less (the repo suite convention — the
tests spec-load this file, no package import required).
"""

import hashlib

NAME_CAP = 64   # the hard cap for ONE mirrored tool name ("64-Cap-Kette")

# Tier-3 scaffolding: "mr__" prefix + one "__" between the two truncated
# parts + one "__" before the digest. Fixed by design (GREEN-room rule:
# the truncation split keeps the name at or under NAME_CAP by construction).
_T3_OVERHEAD = len("mr__") + 2 * len("__")


class MirrorNameError(Exception):
    """The name chain cannot produce a unique effective name set.

    Design: "Kollisionen -> Conformance bricht mit Fehler ab (fail-closed)"
    — the mirror refuses rather than silently picking or mangling a name.
    """


def mirror_tool_name(provider, tool):
    """ONE effective mirrored tool name for the (provider, tool) pair.

    Chain (design § Namensgesetz): tier 1 ``memrack__<provider>__<tool>``;
    over the cap -> tier 2 ``mr__<provider>__<tool>``; still over the cap
    -> tier 3 ``mr__<p>__<t>__<hash4>``. The tier-3 digest is derived
    deterministically from provider+tool alone — never the registration
    order (the alias must survive restarts). The tier-3 parts split the
    remaining budget evenly (deterministic rule, both truncated names
    never exceed their half) and the 4-hex digest closes the name.
    """
    provider = str(provider or "")
    tool = str(tool or "")
    tier1 = "memrack__%s__%s" % (provider, tool)
    if len(tier1) <= NAME_CAP:
        return tier1
    tier2 = "mr__%s__%s" % (provider, tool)
    if len(tier2) <= NAME_CAP:
        return tier2
    # Tier 3: the digest is a pure function of (provider, tool) with an
    # explicit separator byte — no dependence on call order, stable across
    # restarts (byte-identical alias, else the prompt cache breaks).
    digest = hashlib.sha256(("%s\x1f%s" % (provider, tool))
                            .encode("utf-8")).hexdigest()[:4]
    budget = max(0, NAME_CAP - _T3_OVERHEAD - len(digest))
    half = budget // 2
    return "mr__%s__%s__%s" % (provider[:half], tool[:half], digest)


def mirror_tool_names(provider, tools):
    """The mirror set for ONE provider: {original tool name: effective name}.

    Same chain as mirror_tool_name for every entry of the set; the result
    must be handed to ensure_unique() before registration (fail-closed on
    any collision inside the set).
    """
    return {str(tool): mirror_tool_name(provider, tool)
            for tool in (tools or [])}


def ensure_unique(effective_names, *, origin=""):
    """Fail-closed uniqueness over a set of effective mirrored names.

    The conformance law's enforcement seam (design: "Kollisionen ->
    Conformance bricht mit Fehler ab (fail-closed)"): duplicated effective
    names in one mirror registration raise MirrorNameError instead of
    quietly overwriting a sibling tool. ``origin`` must name what was
    checked so the abort is actionable (provider + tool list).
    """
    seen = set()
    for name in (effective_names or []):
        if name in seen:
            raise MirrorNameError(
                "mirrored tool name collision (fail-closed): %r would register "
                "twice — origin: %s (nothing is silently deduplicated)"
                % (name, origin or "unrecorded set"))
        seen.add(name)
    return True


# ── the two tool-schema shapes the ABC allows, read as data ─────────────────

def extract_tool_name(schema):
    """The tool name of ONE schema dict, in EITHER ABC shape.

    The Hermes ABC names two shapes: the function-wrapped
    ``{"type": "function", "function": {"name": ...}}`` form (OpenAI-style,
    this repo's own tools) and the flat ``{"name": ...}`` form named in the
    ABC docstring. The mirror works on BOTH; a nameless shape yields None
    (it registers under nothing and mirrors nothing).
    """
    if not isinstance(schema, dict):
        return None
    function = schema.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    name = schema.get("name")
    return str(name) if name else None


def with_tool_name(schema, new_name):
    """A COPY of one schema carrying the MIRRORED name (shape-preserving).

    Only the name field is rewritten — description, parameters and the
    provider's own envelope travel through untouched (mirror, not rebuild).
    """
    if not isinstance(schema, dict):
        return schema
    function = schema.get("function")
    if isinstance(function, dict):
        out = dict(schema)
        function = dict(function)
        function["name"] = new_name
        out["function"] = function
        return out
    out = dict(schema)
    out["name"] = new_name
    return out
