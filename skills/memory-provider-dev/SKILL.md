---
name: memory-provider-dev
description: "Use when building, auditing or docking a Hermes memory-provider plugin: ABC conformance checks, config-surface pitfalls, fail-closed docking, and proven fixes."
version: 1.0.0
author: Kesuek
license: MIT
metadata:
  hermes:
    tags: [hermes, memory-provider, plugins, conformance, docking]
    related_skills: [gateway-platform-adapter]
---

# Memory-Provider Dev Kit

## When to Use

- Building, auditing, extending or docking a Hermes memory-provider plugin (ABC conformance, config surfaces, hybrid racks, catalog docks).
- Debugging a provider that loads but misbehaves at config, tool-surface or rack level.
- Preparing a memory provider for catalog submission (validate → docs → probes).

Reference implementation: this plugin repo (`memrack/`): `backends/_template.py`, `memrack/sdk.py`, `memrack/conformance.py`, `memrack/config_surface.py`, `memrack/docking.py`, `memrack/namechain.py`, `docs/backend-guide.md`.

## 1. The Five-Minute ABC Check (run it live, never from memory)

1. **Load against the real loader** (MRO/abstract masks explode here, not in unit suites):

```bash
/home/felix/.hermes/hermes-agent/venv/bin/python3 -c "
from plugins.memory import load_memory_provider
p = load_memory_provider('<name>', register_skills=False)
print(p.name, p.is_available())
tools = [s['function']['name'] if 'function' in s else s['name'] for s in p.get_tool_schemas()]
print('tools:', tools)
"
```

The ABC contract (`agent/memory_provider.py`) requires **five members**: `name`, `is_available`, `initialize`, `get_tool_schemas`, `handle_tool_call` (`is_available()` is abstract). Everything else is opt-in hooks (`on_turn`, `on_memory_write`, `on_session_end`, `prefetch`, `system_prompt_block`, `shutdown`, `save_config`, `get_config_schema`).

Mixin rule: **mixin class first**, contract second — `class Bound(MixinBase, MemoryProviderABC)` (an ABC-first listing shadows implementations through MRO). Standalone stub classes (ImportError fallbacks without abstract members) mask gaps green in unit suites; load-ability is proven **only** in the real venv.

2. **Card-vs-code**: every claimed search mode / tool must have an implementation. `from memrack.conformance import check_backend; check_backend(Backend())` — missing delivery = registry error, not a warning.

3. **Config-surface triple check** (all three surfaces, then assert convergence):

```bash
# exactly what the desktop gear pane renders:
/home/felix/.hermes/hermes-agent/venv/bin/python3 -c "
from plugins.memory.config_schema import get_provider_config_schema
sch = get_provider_config_schema('<name>')
print([(f.key, f.kind) for f in sch.fields])
"
```

4. **Fail-closed probe**: dock probes instantiate the provider in isolation (`register_skills=False`); a provider that reports unavailable (missing credentials/config) is refused with a gap note — a draft card is never invented.

## 2. The Three Config Surfaces (and their laws)

| Surface | Loader | Laws |
|---|---|---|
| CLI wizard (`hermes memory setup`) | schema fields as TEXT | list-valued keys ship as comma-STR (normalize at read), count keys keep int defaults (web renders kind=integer); `save_config` must write the same block the provider reads |
| Desktop gear pane (declared) | `config_schema.py` exports `CONFIG_SCHEMA` (pure data) | payload = `_declared_provider_payload(get_provider_config_schema(name))` — run it live; it is **import-cached**: newly installed/docked providers appear after `systemctl --user restart hermes-serve`, toggle effects per `/new` |
| Provider surface (setup modal) | `get_config_schema()` on the instance | the dynamic field family lives here (per-backend membership toggles, dock select); bool/select kinds are proven UI kinds |

Dual-store convergence: `_profile_config` reads the yaml base AND the flat json (`<hermes_home>/<name>/config.json`; flat keys win = freshest user intent); `save_config` mirrors both atomically (tmp file 0600 + `os.replace`, merge per key — a wizard partial-write must never drop unrelated keys, and must never `write_text` a host config).

## 3. Docking Contract (catalog-provider racks)

- **Manifest-first presence** (`backends.d/<name>.yaml`): file exists = registered; `disabled-` prefix = parked (rename, never delete); card = data, human-authored (`best_for`/`limits` proposed until a chat confirmation → reviewed + revisions entry, drift-recheck-immun).
- **Probe**: isolated instantiation, ABC verification, per-provider classification (query-capable vs prefetch-only), evidence (probed_at, provider version). Unavailable → `refused: true` + gap note.
- **Park/restore**: membership toggles park by presence (rename) and restore without re-probe; card data survives every park cycle.
- **Uninstall grace**: uninstalling the provider is detected at init → park immediately with an honest status note (never a silent drop, never an error spam); the card is retained for a configurable window (`dock_retention_days`), then cleaned up to a backup folder; re-install before the window restores membership and keeps review state.

## 4. Mirrored Tools (name law)

Host grammar allows only `A-Za-z0-9_-` (max 64). Chain: `memrack__<provider>__<tool>` → `mr__<p>__<t>` → deterministic hash4 digest suffix (stable across restarts and versions; never registration-order numbering). Collisions fail conformance (fail-closed); effective names are always visible in the status card. Tool schemas surface in the flat ABC shape `{"name", "description", "parameters"}`.

## 5. Threading + Secret Discipline

Background work only via `spawn_context_thread`; secrets via the secret scope / env keys (`load_env`), never raw `os.environ`, never in cards or logs; dock drafts, shims and stores 0600 (dock dir 0700).

## 6. Verify-Commands (reference default)

```bash
# suite (always without PYTHONPATH leakage):
cd <plugin repo> && env -u PYTHONPATH uvx --with pytest --with pyyaml --with ruamel.yaml --with sqlite-vec pytest tests/ -q
# deploy profile copies, then sha-proof each file
# catalog repo-shape check: hermes plugins validate
```

## Pitfalls → Proven Fixes

| Pitfall | Fix |
|---|---|
| Host `config.yaml` clobbered by `yaml.dump` (comments gone, non-atomic) | merge per key + tmp file 0600 + `os.replace` |
| Dual-store drift (yaml vs flat json) | read both (flat wins), mirror both on save |
| Gear pane is import-frozen while docks install late | build `CONFIG_SCHEMA` at module load from a live scan; document the restart-staleness law honestly |
| Two membership UIs contradict visibly | one UI (per-backend toggles); keep the string form internal-only, maintained by the fold |
| Provider uninstalled leaves orphan card + silent rack drop | uninstall-grace: immediate park + honest note; retention-based card cleanup |
| CLI subcommands registered one level too deep | register subcommands directly on the parser argument given to `register_cli` |
| Long/colliding tool names break registration | the 64-cap name chain with deterministic digest; collisions fail conformance |
| `fts5` treats `:`/hyphens as column syntax | pass raw syntax through only for known columns; otherwise quote tokens |
| SQLite extension load fails (`not authorized`) | `enable_load_extension(True)` before loading; degrade honestly on builds without extension support |
| Plain `Lock` deadlocks on reentrant fan-out | `RLock` for reentrant read paths |
