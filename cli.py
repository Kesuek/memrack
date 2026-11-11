"""``hermes memrack <sub>`` — the provider CLI.

Discovery imports ONLY this file at argparse setup (active-provider contract) —
the provider/engine work happens lazily inside the handlers, per invocation,
against THIS profile's HERMES_HOME.

Subcommands:
  status    — rack inventory + active/gaps/parked/pending-docks (memrack_status surface)
  report    — the heat report: hot facts, never-read facts, pointers (Kuratur basis)
  export    — full JSON dump of the base-db store (facts + pointers) — backup/migration
  import    — the legacy import job (builtin memory → fact rows; deterministic, idempotent)
  dock      — catalog-provider docking machine: list offers/dock cards, dock <provider>
              [--probe] (probe → draft card), --refresh (drift-recheck), show/remove
  vec       — the vec document store: `vec add --path <file> [--ref R] [--pages N]`
              ingests a document into the vec corpus (its OWN store, autonomous)
"""

from __future__ import annotations

import argparse
import json


def _provider(home=None):
    """Load + initialize memrack for THIS home (register_skills off: the CLI is a
    read/probe surface, skill side effects stay with the agent runtime)."""
    from plugins.memory import load_memory_provider

    p = load_memory_provider("memrack", register_skills=False)
    p.initialize(session_id="CLI", hermes_home=home, platform="cli")
    return p


def _print(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


_SUBCOMMAND_HELP = (
    ("status", "rack inventory: backends with card, active/gaps/parked/pending-docks"),
    ("report", "heat report: hot top-10 + never-read facts + pointer counts"),
    ("export", "full JSON dump of the base-db store (facts + pointers)"),
    ("import", "legacy import job (builtin memory → fact rows, idempotent)"),
    ("vec", "vec document store: ingest a document into the vec corpus "
            "`vec add --path <file> [--ref <ref>] [--pages N]`"),
    ("dock", "catalog-provider docking machine: list offers/cards, "
             "dock <provider> [--probe|--refresh], show/remove"))


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Build the subcommand tree DIRECTLY on the GIVEN parser — it IS the fresh
    ``hermes memrack`` command parser; adding 'memrack' again was the wrong-node bug
    ('unrecognized arguments: status' — the subcommands landed one level too deep)."""
    parser.set_defaults(func=_run, sub="status")   # bare `hermes memrack` = status
    subs = parser.add_subparsers(dest="sub", metavar="<sub>")
    for name, help_ in _SUBCOMMAND_HELP:
        sub = subs.add_parser(name, help=help_)
        if name == "import":
            sub.add_argument("--source", choices=("memory", "user", "both"), default="both",
                             help="legacy source (default: both)")
        if name == "vec":
            sub.add_argument("verb", nargs="?", default="add",
                             help="add (default: ingest a document)")
            sub.add_argument("--path", default=None,
                             help="document file: .md/.txt read to text, .pdf via pypdf")
            sub.add_argument("--ref", default=None,
                             help="stable reading reference (default: the given path)")
            sub.add_argument("--pages", type=int, default=None,
                             help="pdf-only: limit to the first N pages")
            sub.add_argument("--chunk-chars", dest="chunk_chars", type=int, default=1200)
            sub.add_argument("--chunk-overlap", dest="chunk_overlap", type=int, default=0)
            sub.add_argument("--text", default=None,
                             help="ingest an inline text instead of a file")
        if name == "dock":
            sub.add_argument("verb", nargs="?", default="list",
                             help="list (default) | show | remove | probe | refresh "
                                  "| <provider> (= an offer only, unless --probe)")
            sub.add_argument("item", nargs="?", default=None,
                             help="<provider> for show/remove; `dock <provider>` "
                                  "otherwise")
            sub.add_argument("--probe", action="store_true",
                             help="run the isolated probe and write the draft card")
            sub.add_argument("--refresh", action="store_true",
                             help="drift-recheck the card against the installed "
                                  "provider version (an install that left the "
                                  "house parks its card instead: the uninstall "
                                  "grace)")
        sub.set_defaults(func=_run)


def _docking(home=None):
    """The docking machine module, spec-path resolved beside THIS file and cached
    one-copy per process (the CLI imports ONLY this file at argparse setup — the
    machine itself loads lazily at call time, the same active-provider contract)."""
    import importlib.util
    import pathlib
    import sys
    mod = sys.modules.get("memrack_cli_docking")
    if mod is None:
        path = pathlib.Path(__file__).resolve().parent / "memrack" / "docking.py"
        spec = importlib.util.spec_from_file_location("memrack_cli_docking", str(path))
        if spec is None or spec.loader is None:
            raise ImportError("dock machine not resolvable beside cli.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["memrack_cli_docking"] = mod
        spec.loader.exec_module(mod)
    return mod


def _provider_required(action, item):
    _print({"error": "provider required for %r" % action,
            "hint": "hermes memrack dock %s <provider>" % action})
    return 1


def _run_dock(args, home) -> int:
    docking = _docking(home)
    verb = (args.verb or "list").strip()
    item = (args.item or "").strip()
    if verb in ("show", "remove", "probe", "refresh") and not item:
        return _provider_required(verb, item)
    if verb == "list":
        if args.probe or args.refresh:
            _print({"error": "--probe/--refresh need a provider",
                    "hint": "hermes memrack dock <provider> --probe"})
            return 1
        _print(docking.dock_list(hermes_home=home))
    elif verb == "show":
        _print(docking.dock_show(item, hermes_home=home))
    elif verb == "remove":
        _print(docking.dock_remove(item, hermes_home=home))
    elif verb == "probe":
        _print(docking.dock(item, probe=True, hermes_home=home))
    elif verb == "refresh":
        _print(docking.dock(item, refresh=True, hermes_home=home))
    elif item:
        _print({"error": "too many arguments (%r, %r)" % (verb, item),
                "hint": "hermes memrack dock [list|show|remove|probe|refresh] "
                        "[<provider>] — or `dock <provider> [--probe]`"})
        return 1
    else:
        _print(docking.dock(verb, probe=args.probe, refresh=args.refresh,
                            hermes_home=home))
    return 0


def _run(args) -> int:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    if args.sub == "dock":    # the docking machine needs no provider load
        try:
            return _run_dock(args, home)
        except Exception as e:
            _print({"error": f"memrack dock failed: {type(e).__name__}: {e}",
                    "hint": "the machine is fail-closed — nothing was docked"})
            return 1
    try:
        p = _provider(home)
    except Exception as e:  # fail with an actionable message, not a traceback wall
        _print({"error": f"memrack not loadable: {type(e).__name__}: {e}",
                "hint": "'hermes memory status' for install/availability state"})
        return 1

    try:
        if args.sub == "vec":
            return _run_vec(args, p)
        if args.sub == "status":
            _print(json.loads(p.handle_tool_call("memrack_status", {})))
        elif args.sub == "import":
            _print(json.loads(p.handle_tool_call("memrack_import", {"source": args.source})))
        else:
            # engine-level read of the LIVE store through the provider's rack
            eng = getattr(p, "_rack", {}).get("base-db")
            db = getattr(eng, "_db", None) if eng else None
            if db is None:
                _print({"error": "no base-db store in the rack (check enabled_backends)"})
                return 1
            if args.sub == "report":
                hot = db.execute(
                    "SELECT id, substr(content,1,90), target, hits, ts FROM facts"
                    " ORDER BY hits DESC, ts DESC LIMIT 10").fetchall()
                cold = db.execute("SELECT COUNT(*) FROM facts WHERE hits = 0").fetchone()[0]
                total = db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                ptrs = db.execute("SELECT COUNT(*) FROM pointers").fetchone()[0]
                _print({"facts_total": total, "facts_never_read": cold, "pointers_total": ptrs,
                        "hot_top10": [{"id": r[0], "content": r[1], "target": r[2],
                                       "hits": r[3], "ts": r[4]} for r in hot]})
            else:  # export
                _print({"home": str(home),
                        "facts": [{"id": r[0], "content": r[1], "target": r[2],
                                   "ts": r[3], "hits": r[4]} for r in db.execute(
                                       "SELECT id, content, target, ts, hits FROM facts ORDER BY id")],
                        "pointers": [{"id": r[0], "subject": r[1], "location": r[2], "ts": r[3]}
                                     for r in db.execute(
                                         "SELECT id, subject, location, ts FROM pointers ORDER BY id")]})
    finally:
        p.shutdown()
    return 0


def _run_vec(args, p) -> int:
    """`hermes memrack vec add` — ingest into the vec store's OWN corpus."""
    if (args.verb or "add").strip() not in ("add", ""):
        _print({"error": "unknown vec verb %r" % args.verb,
                "hint": "hermes memrack vec add --path <file> [--ref <ref>] [--pages N]"})
        return 1
    if not args.path and not args.text:
        _print({"error": "nothing to ingest: give --path <file> (or --text)",
                "hint": "e.g. hermes memrack vec add --path ~/wiki/notes.md"})
        return 1
    payload = {"source_ref": (args.ref or args.path or ""),
               "chunk_chars": args.chunk_chars, "chunk_overlap": args.chunk_overlap,
               "pages": args.pages}
    payload["path" if args.path else "text"] = args.path or args.text
    out = json.loads(p.handle_tool_call("memrack_vec_add", payload))
    _print(out)
    if isinstance(out, dict) and out.get("error"):
        return 1
    return 0


def memrack_command(args) -> int:
    """The module-level handler the memory-CLI discovery wires (handler_fn contract)."""
    return _run(args)
