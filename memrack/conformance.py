"""memrack conformance — one code path validates the docking contract
(used by tests, the registry AND the standalone runner: python -m memrack.conformance <backend.py>)
"""

import importlib.util
import json
from pathlib import Path


def _err(errors, key, message, **kv):
    entry = {"kind": key, "error": message}
    entry.update(kv)
    errors.append(entry)
    return entry


def check_backend(obj, *, tmp_home=None) -> dict:
    """Validate ANY backend object against the docking contract (structural + card +
    optional write/search smoke). Return dict: {ok, errors[], warnings[]}."""
    errors = warnings = []
    report = {"errors": (errors := []), "warnings": (warnings := [])}

    # 1) required surface (name = attr, not callable — checked separately as the card)
    for member in ("initialize", "on_turn", "on_memory_write", "on_session_end",
                   "prefetch", "system_prompt_block", "shutdown", "export"):
        if not callable(getattr(obj, member, None)):
            _err(errors, "contract", "misses %s" % member)
    if not getattr(obj, "name", None):
        _err(errors, "card", "name empty")

    # 2) card honesty (claimed modes need the engine)
    modes = None
    dyn = getattr(obj, "card_modes", None)
    modes = tuple(dyn()) if callable(dyn) else tuple(getattr(obj, "search_modes", ()))
    if modes and not callable(getattr(obj, "search", None)):
        _err(errors, "card-lie", "claims search modes %r without a search method" % (modes,))
    if not getattr(obj, "best_for", None):
        warnings.append("card: best_for empty — the agent routes blind-ish")
    if not getattr(obj, "limits", None):
        warnings.append("card: limits empty")

    # 3) write/idempotence/search smoke (no network, tmp store only)
    if callable(getattr(obj, "write", None)) and callable(getattr(obj, "initialize", None)) \
            and not errors:
        try:
            work_home = str(tmp_home or Path("/tmp/memrack-conform-probe").resolve()) \
                .replace("/dev/null", "/tmp/memrack-conform-probe")
            obj.initialize(session_id="CONFORM", hermes_home=work_home, provider_config={})
            probe_text = "conformance probe %r" % work_home
            first = obj.write("explicit", probe_text)
            if not (isinstance(first, dict) and first.get("target")):
                _err(errors, "write-smoke", "write did not return {status, target}")
            second = obj.write("explicit", probe_text)
            if isinstance(first, dict) and first.get("id") and second.get("id"):
                if first.get("id") != second.get("id"):
                    _err(errors, "idempotence", "same content produced different ids")
            if callable(getattr(obj, "search", None)):
                for mode in [m for m in ("fts",) if m in modes]:
                    hits = obj.search(probe_text.split()[0], mode, 5) or []
                    for hit in hits:
                        for field in ("backend", "kind", "rank", "target", "ts"):
                            if field not in hit:
                                _err(errors, "hit-shape", "hit misses %r" % field)
                                break
                        if hit.get("kind") == "fact" and "hits" not in hit:
                            warnings.append(
                                "hit-shape: fact-hit without 'hits' silently loses the "
                                "heat tiebreak (A-6) — supply the field")
            obj.shutdown()
        except NotImplementedError as exc:
            _err(errors, "contract", "initialize NotImplementedError: %s" % exc)
        except Exception as exc:
            _err(errors, "smoke", "%s: %s" % (type(exc).__name__, exc))

    report["ok"] = not errors
    return report


def assert_conformance(obj, *, tmp_home=None):
    report = check_backend(obj, tmp_home=tmp_home)
    assert report["ok"], json.dumps(report, indent=2, ensure_ascii=False)
    return report


def _apply_sidecar_manifest(obj, path) -> dict:
    """Docking parity: the runner applies an adjacent MANIFEST and checks the
    CLAIMS (claimed modes without implementation = same registry-error).
    Without sidecar: pure file-level check (card = code-attrs)."""
    import yaml
    report = {"manifest": None, "card_source": "code-attrs"}
    for cand in (path.with_suffix(".yaml"), path.with_suffix(".yml")):
        if cand.exists():
            man = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
            obj.manifest = man
            report["manifest"] = cand.name
            report["card_source"] = "manifest(+code-verify)"
            dyn = getattr(obj, "card_modes", None)
            live = tuple(dyn()) if callable(dyn) else tuple(getattr(obj, "search_modes", ()))
            for claimed in (man.get("search") or ()):
                if not callable(getattr(obj, "search", None)):
                    report["errors"].append({"kind": "card-lie",
                                             "error": "manifest claim %r without a search method" % claimed})
            # falsy-tuple trap: live==() is an HONEST card (root unconfigured) —
            # every claim is then un-armed, not "no check"
            report["declared_not_armed"] = [m for m in (man.get("search") or ()) if m not in live]
            break
    return report


def _run(path, tmp_home=None) -> int:
    path = Path(path)
    spec = importlib.util.spec_from_file_location("memrack_conformance_target", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    obj = mod.export_backend()
    manifest_info = _apply_sidecar_manifest(obj, path)
    report = check_backend(obj, tmp_home=tmp_home)
    report.update(manifest_info)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else None
    home = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(_run(target, home) if target else 2)
