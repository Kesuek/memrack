"""Config-surface seams for the docked rack: dynamic membership toggles, the
dock_want select + its initialize() consumption, the pending-confirmation
plumbing and the card-review writer (T-036, GREEN).

Design-Contract (source of truth):
~/.hermes/projects/memrack/references/catalog-rack-adapter-design.md,
section "## Addendum (2026-11-08): Config-Surface + Pending-Confirmation-Flow"
— the frozen-adapter supplement. Every seam below cites the exact Addendum
point it serves; nothing here invents laws beyond it: the Basisfall stays
untouched (base-db is the floor, wiki parked by default), no auto-dock
besides the explicit config choice, cards stay human-authorized at review.

GREEN state (T-036 coder phase): all six seams implemented —
``membership_fields`` / ``merge_membership_toggle`` /
``set_docked_membership`` / ``dock_want_field`` / ``dock_want_hook`` /
``pending_flags`` / ``review_card`` against the contracts in
tests/test_config_dock_surface.py (RED->GREEN proven in the run report).
The provider core wires them: get_config_schema instance derivation, the
initialize() hook call, the status/cards-slot plumbing, the
``memrack_card_review`` tool surface.

Pure stdlib at module level (repo suite convention: tests spec-load this
file hermes-less; pyyaml is imported lazily inside the implementations,
exactly like the docking machine does it).
"""

import os
from pathlib import Path

PROPOSED_CARD_TEXT = "PLACEHOLDER —"
"""The draft-card best_for/limits prefix the docking machine writes (the
honest PROPOSED marker, never machine-guessed content); the pending
detection reads this state from the card, it does not re-derive it."""

_ENABLED_DEFAULT = ("base-db", "state-episodic")
"""Mirror of the provider core's `_DEFAULT_ENABLED` (the plugin-shipped
default): state-episodic default-ON, wiki opt-in (the parked-by-default law).
The core reads/writes through its OWN `_norm_backends`; this stand is the
toggle default's source for pure-seam callers (the mirrored-source note in
each docstring)."""

_TOGGLE_SUFFIX = "_in_rack"


def _truth(value):
    """Read-side truth for toggles — the provider core's `_truthy` semantics
    mirrored (wizard "true"/"false" strings AND native bools mean the same;
    the catalog proxy keeps the identical `_truth` mirror)."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "yes", "on", "1")


def _enabled_stand(value):
    """`_norm_backends` semantics mirrored from the provider core (the
    source-of-truth law): ONE wizard string or a hand-edited yaml list must
    rack identically; unknown names are NOT filtered (the registry parks
    honestly); absent/empty falls back to the shipped default stand."""
    if isinstance(value, str):
        items = [t.strip() for t in value.split(",") if t.strip()]
        return items or list(_ENABLED_DEFAULT)
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return list(_ENABLED_DEFAULT)


def _utcnow_iso():
    """UTC, microseconds, Z-suffix — the repo-wide evidence timestamp law
    (identical format to the core/docking machine, string-comparable)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _docking():
    """The docking machine module, spec-path resolved beside THIS file and
    cached one-copy per process under the SAME sys.modules key the provider
    core uses ("memrack_docking_core") — no parallel machine, no parallel
    path computation: cards, shim writes and refusals stay the machine's."""
    import importlib.util
    import sys
    mod = sys.modules.get("memrack_docking_core")
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        "memrack_docking_core",
        Path(__file__).resolve().parent / "docking.py")
    if spec is None or spec.loader is None:
        raise ImportError("docking machine not resolvable beside the seams")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memrack_docking_core"] = mod
    spec.loader.exec_module(mod)
    return mod


def membership_fields(config, built_in_names, docked_names):
    """Dynamic per-backend membership toggle fields (Addendum (1), sentence 1).

    Contract sentence (verbatim):

        'get_config_schema() wird instanz-beziehbar — je Backend (built-ins +
        DOCKED aus backends.d) ein bool-Feld `<name>_in_rack` (default
        passend zum aktuellen membership-Stand).'

    ``config`` is the provider's merged config dict — the membership stand
    of a built-in is read with the provider core's ``_norm_backends``
    semantics (the source-of-truth law). ``built_in_names`` are the
    candidate code-first built-ins (rack members AND parked ones); their
    toggle defaults follow ``enabled_backends``. ``docked_names`` are the
    ACTIVE docked cards (backends.d presence semantics); their toggle
    default is truthy — a docked card racks by presence. A name in BOTH
    lists renders ONCE (docked presence wins the default).

    Returns ONE field per name, wizard shape {"key", "description",
    "default", "choices"} with the closed bool choices pair, sorted by name
    (deterministic, restart-stable — plan T-036: 'stabil sortiert'). The
    static schema keys (backend, enabled_backends, ...) are NOT echoed
    here — the field set is strictly the dynamic `<name>_in_rack` family.
    """
    enabled = set(_enabled_stand((config or {}).get("enabled_backends")))
    built = {str(n).strip() for n in (built_in_names or [])
             if str(n or "").strip()}
    docked = {str(n).strip() for n in (docked_names or [])
              if str(n or "").strip()}
    names = sorted(built | docked)
    fields = []
    for name in names:
        # docked presence wins a mixed candidate: the card racks by presence,
        # the enabled-backends stand only governs built-ins
        is_docked = name in docked
        default = "true" if (is_docked or name in enabled) else "false"
        fields.append({
            "key": name + _TOGGLE_SUFFIX,
            "label": "%s in the rack" % name,
            "description": "Keep %s in the memrack rack (built-ins ride "
                           "enabled_backends; docked cards park/unpark their "
                           "card file)" % name,
            "default": default,
            "choices": ["true", "false"],
        })
    return fields


def merge_membership_toggle(config, key, value):
    """Built-in half of the toggle write-path (Addendum (1), sentence 2).

    Contract sentence (verbatim):

        'UI-Toggle on/off; _norm_backends bleibt Source-of-truth (Toggle
        schreibt die Str-Felder konsistent; Basisfall-Gesetze bleiben).'
        + plan T-036 scope 1: 'Schreibpfad = konsistenter Update von
        enabled_backends/docks (dedupe, Basisfall-Gesetze bleiben:
        base-db nie komplett weg, wiki geparkt-default).'

    ``key`` is one dynamic toggle key (<name>_in_rack), ``value`` is the
    toggle's new state in either shape (wizard strings true/false or a
    native bool — read with the provider core's ``_truthy`` semantics).
    Returns a NEW config dict (the input stays untouched — pure seam)
    whose ``enabled_backends`` carries the consistent, DEDUPLICATED
    membership update as ONE string (the wizard convention ``_norm_backends``
    reads); every other key rides along untouched. The base case stays
    anchored: a ``base-db`` toggle-off is absorbed (the floor never leaves
    the rack), and the write-path never silently flips UNRELATED parked
    members in (wiki stays parked by default — only the toggled name moves).
    """
    cfg = dict(config or {})
    key = str(key or "")
    if not key.endswith(_TOGGLE_SUFFIX):
        raise ValueError("not a membership toggle key: %r (expected the "
                         "<name>%s shape)" % (key, _TOGGLE_SUFFIX))
    name = key[: -len(_TOGGLE_SUFFIX)].strip()
    if not name:
        raise ValueError("membership toggle key %r carries no backend name" % key)
    on = _truth(value)
    enabled = _enabled_stand(cfg.get("enabled_backends"))
    if on:
        # dedupe: remove every duplicate of THIS name, then add it once —
        # unrelated members keep their positions (no silent reshuffle)
        enabled = [b for b in enabled if b != name] + [name]
    elif name != "base-db":
        enabled = [b for b in enabled if b != name]
    # the floor law: a base-db toggle-off is absorbed (base-db is the floor
    # and never leaves the rack completely)
    cfg["enabled_backends"] = ",".join(enabled)
    return cfg


def set_docked_membership(provider_name, member, *, hermes_home=None):
    """Docked half of the toggle write-path (plan T-036 scope 1 —
    'Schreibpfad = konsistenter Update von enabled_backends/docks').

    A docked card's membership is PRESENCE (the registry law: the card
    file being there = registered). Toggling a docked member off therefore
    parks the card — the established ``disabled-<provider>.yaml`` prefix
    rename convention (an existing docking-law file operation, no removal,
    no separate machine state); toggling on renames it back. Unknown card
    name = controlled error ({"error": ...}), nothing written. The card
    and shim stay PRIVATE (0600 file, 0700 card dir — a rename preserves
    the file's mode). The shim byte stream is never touched (card edits
    never need a shim rewrite).
    """
    name = str(provider_name or "").strip()
    if not name:
        return {"error": "docked membership toggle needs the card's name",
                "hint": "memrack_status cards show the docked names"}
    machine = _docking()
    dock_dir = machine._docked_dir(hermes_home)
    wanted_off = ("disabled-%s.yaml" % name, "disabled-%s.yml" % name)
    wanted_on = ("%s.yaml" % name, "%s.yml" % name)
    active = next((dock_dir / candidate for candidate in wanted_on
                   if (dock_dir / candidate).exists()), None)
    parked = next((dock_dir / candidate for candidate in wanted_off
                   if (dock_dir / candidate).exists()), None)
    on = _truth(member)
    if on:
        if active is not None:
            return {"provider": name, "member": True, "card": active.name,
                    "note": "already in the rack (presence semantics, idempotent)"}
        if parked is None:
            return {"error": "no dock card on file for %r" % name,
                    "hint": "memrack_status pending_docks / `memrack dock list` "
                            "show what is docked or parked"}
        try:
            restored = dock_dir / (name + (parked.suffix or ".yaml"))
            os.replace(parked, restored)
        except OSError as exc:
            return {"provider": name, "member": True,
                    "error": "card %r could not be restored: %s" % (parked.name, exc)}
        return {"provider": name, "member": True, "card": restored.name,
                "parked": parked.name,
                "note": "card restored — presence semantics: it racks on the "
                        "next discovery (this or the next initialize)"}
    if parked is not None:
        return {"provider": name, "member": False, "parked": parked.name,
                "card": None,
                "note": "already parked under the disabled- prefix (idempotent)"}
    if active is None:
        return {"error": "no dock card on file for %r" % name,
                "hint": "memrack_status pending_docks / `memrack dock list` "
                        "show what is docked or parked"}
    try:
        parked_path = dock_dir / ("disabled-%s%s" %
                                  (name, active.suffix or ".yaml"))
        os.replace(active, parked_path)
    except OSError as exc:
        return {"provider": name, "member": False,
                "error": "card %r could not be parked: %s" % (active.name, exc)}
    return {"provider": name, "member": False, "parked": parked_path.name,
            "card": active.name,
            "note": "card parked under the disabled- prefix (presence "
                    "semantics: without deletion; the shim stays untouched)"}


def dock_want_field(offers):
    """The dock_want select field (Addendum (2), sentences 1-2).

    Contract sentence (verbatim):

        'dock_want (select): choices = offers (installierte, inaktive
        Katalog-Provider) + "" (none). Beim nächsten initialize():
        dock_want gesetzt → probe+draft-card+shim via die EXISTIERENDE
        Docking-Maschine (die Konfig-Wahl IST die User-Entscheidung — kein
        heimliches Auto-Dock).'

    ``offers`` is the offer list in the docking machine's catalog_offers
    shape ({name, source, version, dir, active}) — the provider hands the
    ALREADY-deduped list (the machine's own offer filtering stays where it
    is; this seam does not re-filter). Returns the wizard-shaped select
    field dict whose choices carry every offer name PLUS ``""`` (none) in
    a deterministic order (sorted, stable across restarts). An empty
    offer list still renders the field with the one none choice (the
    honest hermes-less/discovered-nothing form).
    """
    names = sorted({str((offer or {}).get("name") or "").strip()
                    for offer in (offers or [])} - {""})
    return {
        "key": "dock_want",
        "label": "Dock a catalog provider",
        "description": "Dock one installed, INACTIVE catalog memory provider "
                       "into the rack at the next initialize (empty = none — "
                       "no auto-dock; the probe writes a DRAFT card whose "
                       "card text then needs the chat confirm)",
        "default": "",
        "choices": [""] + names,
    }


def dock_want_hook(config, hermes_home=None, docking=None, offers=None,
                   loader=None):
    """initialize()-consumption of dock_want (Addendum (2), sentence 2).

    Contract sentence (verbatim):

        'Beim nächsten initialize(): dock_want gesetzt → probe+draft-card
        +shim via die EXISTIERENDE Docking-Maschine (die Konfig-Wahl IST
        die User-Entscheidung — kein heimliches Auto-Dock).'

    ``config`` (the provider's merged config dict) carries the choice.
    Empty or absent → NOTHING is docked (None returned — the provider
    racks exactly as without the field; no heimliches Auto-Dock). A
    non-empty choice is handed to the EXISTING machine (no parallel
    implementation): its dock(probe=True) flow runs probe + draft card +
    shim, and its own fail-closed refusals stand (a provider that is not
    an installed, inactive offer is refused, and a REVIEWED card on file
    is never overwritten — the machine's laws, not re-rules here).

    ``docking`` is the machine module seam (None = resolved beside this
    file via the same one-copy cache the provider core uses); ``offers``
    and ``loader`` are the machine's own documented test seams, forwarded
    untouched. The machine's receipt (or refusal report) is returned
    honestly — the caller decides the surfacing.
    """
    choice = str((config or {}).get("dock_want") or "").strip()
    if not choice:
        return None
    machine = docking if docking is not None else _docking()
    return machine.dock(choice, probe=True, loader=loader, offers=offers,
                        hermes_home=hermes_home)


def pending_flags(card):
    """Per-card Pending-Confirmation flags (Addendum (3), sentence 1).

    Contract sentence (verbatim):

        'Cards mit PROPOSED-best_for/limits (draft:true) tragen in
        memrack_status und im cards-Slot zusätzlich pending_confirmation:
        true + pending_hint — Meldung an den Agenten auf JEDER
        Tool-Antwort; endgültige Festlegung per Chat bestätigt und per
        Tool gesetzt.'

    ``card`` is one rack card in the core's `_card()`/manifest shape (a
    draft card carries ``draft: True`` and the PROPOSED placeholder texts
    until the human review ran; detection keys on the draft marker's
    truth — the state the machine wrote, never re-derived from text).
    For such a card this returns the additive slot
    {"pending_confirmation": True, "pending_hint": <str>} — the
    agent-facing message naming the confirmation mechanism (chat confirm +
    the memrack_card_review tool), never inventing card text. A card
    whose review ran carries NO flags ({}) — the flags appear on final
    text never.
    """
    if not isinstance(card, dict) or not _truth(card.get("draft")):
        return {}
    name = str(card.get("name") or "this provider")
    hint = (
        "PROPOSED card (draft): its best_for/limits are placeholders the "
        "docking machine wrote — confirm in chat, then set the final "
        "reviewed texts via the memrack_card_review tool "
        "(backend=%s, best_for=..., limits=...); until then treat its "
        "claims as unconfirmed." % name)
    return {"pending_confirmation": True, "pending_hint": hint}


def review_card(provider_name, best_for, limits, *, hermes_home=None):
    """The memrack_card_review tool's card writer (Addendum (4)).

    Contract sentence (verbatim):

        'memrack_card_review Tool: args backend + best_for + limits →
        setzt reviewte Texte, draft→reviewed, revisions-Entry (timestamp,
        source=chat-confirm). Unbekanntes Backend / leere Texte = controlled
        error. Reviewed Cards sind Drift-Recheck-immun (existierende Law).'

    ``provider_name`` is the DOCKED backend's card name (the card file
    backends.d/<provider>.yaml beside its shim); ``best_for``/``limits``
    are the human-reviewed texts, set VERBATIM (never paraphrased, never
    stripped — the texts are the human authorization). The card is
    rewritten through the docking machine's atomic-write discipline
    (the machine's own _atomic_write: 0600 file, 0700 dir), the ``draft``
    marker clears (draft→reviewed) and ONE revisions entry lands:
    {"timestamp": ..., "source": "chat-confirm"} (the Addendum's literal
    key names).

    An unknown card name or an empty (whitespace-only) text is a
    CONTROLLED error — an honest {"error": ...} report, nothing written,
    the card file stays exactly as it was (fail-closed, no partial
    write). The shim byte stream is never touched (card edits never need
    a shim rewrite). The result gains the drift-recheck immunity of every
    reviewed card (the machine's existing law: a probe draft never
    overwrites human card text, and a refresh keeps human-authored
    best_for/limits).
    """
    name = str(provider_name or "").strip()
    best = "" if best_for is None else str(best_for)
    lim = "" if limits is None else str(limits)
    if not name:
        return {"error": "card review needs the docked backend's name",
                "hint": "memrack_status cards name the docked backends"}
    if not best.strip() or not lim.strip():
        return {"error": "card review needs BOTH texts non-empty: best_for "
                         "and limits (empty or whitespace-only refuses — "
                         "the card never gets an empty claim)",
                "provider": name}
    machine = _docking()
    dock_dir = machine._docked_dir(hermes_home)
    card_path = None
    for suffix in (".yaml", ".yml"):
        candidate = dock_dir / (name + suffix)
        if candidate.exists():
            card_path = candidate
            break
    if card_path is None:
        for suffix in (".yaml", ".yml"):
            if (dock_dir / ("disabled-%s%s" % (name, suffix))).exists():
                return {"error": "the card of %r is parked (toggled out of the "
                                 "rack) — restore its membership first"
                        % name, "provider": name}
        return {"error": "no dock card on file for %r" % name,
                "hint": "memrack_status cards name the docked backends — "
                        "the review writes an EXISTING card, never a new one"}
    try:
        card = machine._read_card(card_path)
    except Exception as exc:
        return {"provider": name,
                "error": "unreadable card %s: %s: %s (fail-closed — nothing "
                         "written)" % (card_path.name, type(exc).__name__, exc)}
    if not isinstance(card, dict):
        return {"provider": name,
                "error": "card %s carries no mapping (fail-closed — nothing "
                         "written)" % card_path.name}
    card["best_for"] = best
    card["limits"] = lim
    card.pop("draft", None)
    prior = card.get("revisions")
    revisions = list(prior) if isinstance(prior, list) else []
    revisions.append({"timestamp": _utcnow_iso(), "source": "chat-confirm"})
    card["revisions"] = revisions
    import yaml
    machine._atomic_write(card_path, yaml.dump(
        card, default_flow_style=False, allow_unicode=True, sort_keys=False))
    return {"provider": name, "status": "reviewed", "card": card_path.name,
            "best_for": best, "limits": lim, "revisions": len(revisions),
            "note": "reviewed texts set verbatim, draft cleared, one "
                    "chat-confirm revision recorded; the card is now "
                    "drift-recheck-immun (a refresh never touches these "
                    "texts)"}
