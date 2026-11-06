# BACKEND_GUIDE — write a memrack backend in 15 minutes

Docking needs NO SDK: any module exposing `export_backend()` (returning an object
with the contract members) is a backend. The SDK (`memrack.sdk.MemrackBackend`)
is optional sugar — it pre-builds the fts5/lock/degrade lessons of this suite.

## The contract in one table

| member | required? | purpose |
|---|---|---|
| `name` (attr) | yes | unique backend id in the rack |
| `best_for` / `limits` (attrs) | card | what the agent sees — one sentence each |
| `initialize(session_id=, hermes_home=, provider_config=, **kw)` | yes | once per session; storage under `<hermes_home>/plugin-data/memrack/` |
| `on_turn / on_memory_write / on_session_end / prefetch / system_prompt_block / shutdown` | hooks | no-op defaults are legal |
| `search(query, mode, k) -> [hit]` | engine (optional) | ranked hits; hit = `{backend, kind, rank, target, ts}` (reference, never content copy) |
| `write(kind, content, tags=, session_id=) -> {status, id, target}` | engine (optional) | atomic fact store; hash-dedupe = idempotence law |
| `touch(ids)` | engine (optional) | heat counter, the report basis |
| `export_backend()` at MODULE level | yes | what the registry instantiates |

## 4 steps

1. **Copy the template** `backends/_template.py` out of the repo, rename the class +
   `name`, fill the card (`best_for`, `limits`, `search_modes`).
2. **Prove it**: `python -m memrack.conformance <your>_<file>.py` (from the repo
   checkout) — or in pytest: `from memrack.conformance import assert_conformance`.
   The run checks structure + card honesty + write/idempotence/search smoke.
3. **Dock it**: drop the file into
   `$HERMES_HOME/plugins/memrack/backends.d/` + a manifest `<name>.yaml`
   (`name`, `module` required; `best_for`, `limits`, `search` optional — manifest
   values win as card data). Presence = registered (next session).
4. **See it live**: next session's `memrack_status` lists your card;
   `memrack_search(backend="<name>")` targets it explicitly.

## Contract laws the registry and merge expect (post-R2, binding)

1. **ts = UTC-Z with microseconds** (`2026-11-06T01:25:16.123456Z`) — the merge
   tiebreak compares ts as STRINGS; a zone-less local backend breaks chronology
   silently the moment a second backend writes from another zone.
2. **Fact-hits carry `hits`** (the row's heat counter) — without it your backend
   loses the heat tiebreak silently against base-db. Conformance warns.
3. **target identifies the ENTRY, not the file** — the RRF collapse merges by
   target; a collective target would swallow N−1 of your hits. Import/one-shot
   loads suffix per entry (hash8).
4. **Subclassing the ABC via a mixin: mixin base FIRST** —
   `class Bound(MixinBase, MemoryProvider)` — the ABC-first order shadows
   implementations with declarations (the MRO-shadow lesson, proven live).
5. **`is_available()` is an ABC obligation** (`agent/memory_provider.py:117`,
   abstract) — config/deps gate, no network in it. `get_config_schema()` is what
   `hermes memory setup` reads.

## The design laws you inherit (non-negotiable)

- **No LLM judge at write time** — routing is source/type-based; an autonomous
  LLM-routing process was removed from this project as broken.
- **The card never lies** — a claimed `search` mode without the method, or an
  env-lost mode silently promised, is a registry/degrade error, never a silent empty.
- **All storage under `<hermes_home>/plugin-data/memrack/`** (profile-scoped, 0600-ish
  private dirs) — never `$HERMES_HOME/cache/` (scratch prunes!), never hardcoded homes.
- **EPISODIC turns are NOT copied** — Hermes state.db is canonical; only facts/pointers
  go into backends.
- **No server DB without a documented trigger** (multi-host access, scale evidence,
  benchmark win, or another project's need) — embedded first is the project stance.

## Pitfalls pre-built in the SDK (reuse, don't relearn)

- fts5 reads `"Hit-me"` as column syntax — always quote tokens: `fts_match_tokens()`.
- JOIN over external-content FTS: qualify EVERY column, also `ORDER BY <table>.rank`.
- SQLite extension load needs `enable_load_extension(True)` before `sqlite_vec.load`.
- A locked method calling a locked method = deadlock — the base uses an RLock.
- `load_memory_provider` fresh-instantiates with skills off unless you ask.
