"""``hermes memrack <sub>`` — the provider CLI.

Discovery imports ONLY this file at argparse setup (active-provider contract) —
the provider/engine work happens lazily inside the handlers, per invocation,
against THIS profile's HERMES_HOME.

Subcommands:
  status    — rack inventory + active/gaps/parked/pending-docks (memrack_status surface)
  report    — the heat report: hot facts, never-read facts, pointers (Kuratur basis)
  export    — full JSON dump of the base-db store (facts + pointers) — backup/migration
  import    — the legacy import job (builtin memory → fact rows; deterministic, idempotent)
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
    ("import", "legacy import job (builtin memory → fact rows, idempotent)"))


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
        sub.set_defaults(func=_run)


def _run(args) -> int:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    try:
        p = _provider(home)
    except Exception as e:  # fail with an actionable message, not a traceback wall
        _print({"error": f"memrack not loadable: {type(e).__name__}: {e}",
                "hint": "'hermes memory status' for install/availability state"})
        return 1

    try:
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


def memrack_command(args) -> int:
    """The module-level handler the memory-CLI discovery wires (handler_fn contract)."""
    return _run(args)
