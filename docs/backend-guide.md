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
| `search(query, mode, k) -> [hit]` | engine (optional) | ranked hits; hit = `{backend, kind, rank, target, ts}`; fact-hits ALSO carry `hits` (heat law); optional extras (`snippet`, `content`, `distance`) are LEGAL. **kind vocabulary: `fact` = rows YOUR OWN store holds (heat-counted), `page` = your own markdown whole-pages, `pointer` = reference hits over FOREIGN files/bodies (writeless backends — no heat obligate). The target identifies ONE entry (the collapse law!)** |
| `write(kind, content, tags=, session_id=) -> {status, id, target}` | engine (optional) | atomic fact store; hash-dedupe = idempotence law |
| `touch(ids)` | engine (optional) | heat counter, the report basis |
| `export_backend()` at MODULE level | yes | what the registry instantiates |

Hook fan-out: the provider runs every hook at EVERY rack member — keep hooks
cheap and idempotent. Blocks merge non-empty + dedupe (blank-line join); a
single hook result keeps its exact form, several results pack per
`{backend: result}`; one member's failure is isolated (the others still run).

## Catalog docks (mirror-not-rebuild)

An INSTALLED, INACTIVE catalog memory provider docks through the docking
machine — NOT by writing a backend: `memrack dock <provider> --probe`
(= the `memrack_dock` tool, action=probe) instantiates the provider class in
isolation, proves the MemoryProvider-ABC and writes a DRAFT card + per-provider
binding shim into `backends.d/` (presence semantics: registered next session).
The provider never talks to the host directly — memrack mirrors it: recall
enters the ONE prefetch fusion, the static block concats behind an origin
line, its tools surface UNDER THE MEMRACK NAMESPACE by the 64-cap name chain
(`memrack__<p>__<t>` → `mr__<p>__<t>` → `mr__<p>__<t>__<hash4>`,
deterministic + restart-stable; the effective names always sit on the
`memrack_status` cards). If YOUR backend exposes `tool_schemas`, the mirror
law applies to you too: ONE effective name claimed twice = conformance abort
(fail-closed collision, nothing is silently deduplicated). Decisions
(mirror on/off, write-forwards) are CARD data — recorded keys act, absent
keys fail closed; `best_for`/`limits` stay human-authored (draft
placeholders), and no probe ever overwrites a reviewed card — `--refresh`
re-derives from a fresh probe on a provider version change instead.

### The card-recorded query bridge (fan-out rail)

A provider whose card records BOTH a mode claim and a `query_bridge` recipe
becomes a fan-out rail: `memrack_search(mode=all)` reaches the docked backend
through the bridge, and its hits ride the SAME merged pool (ranked by the
fusion law, collapsed per target, `found_by` naming the rail). The adapter
EXECUTES the recipe and never guesses a provider's query signature — a card
claiming modes without a recorded bridge keeps answering an honest error.

```yaml
# backends.d/<name>.yaml (card data — authored at the dock review)
search: [vec]              # which memrack rail the bridge serves
query_bridge:
  via: tool                # the execution rail (tool call)
  tool: <original_tool>    # the provider's search-family tool, ORIGINAL name
  query_arg: query         # args key that carries the query (default "query")
  # args: {}               # additional static args for the tool
  results_key: results     # list key in the tool's JSON answer (default)
  kind: fact               # the hit kind label in the fused pool (default)
```

The bridge calls the provider's `handle_tool_call` with the ORIGINAL tool
name (the provider never sees the mirrored namespace). Failure discipline:
a tool that answers an `{"error": ...}` payload surfaces as an engine error
in the `memrack_search` answer (never a silent zero); the provider's own
abstain shape (empty results list) is an honest zero; `id`/`target`/`ts`
fields of returned items ride along when the tool supplies them. A
`--refresh` drift recheck NEVER erases recorded card claims it cannot
re-derive (modes/bridge/human text survive a version hop; only a fresh probe
that actually derives modes replaces them).

## 4 steps

1. **Copy the template** `backends/_template.py` out of the repo, rename the class +
   `name`, fill the card (`best_for`, `limits`, `search_modes`).
2. **Prove it**: `python -m memrack.conformance <your_file>.py [scratch_home]`
   from the repo checkout — arg 2 is a scratch HERMES_HOME for the smoke's temp
   storage (default: a /tmp probe dir; use your repo's scratch, NEVER a live home).
   A sibling `<stem>.yaml`/`<name>.yaml` manifest is APPLIED like docking would:
   claims are verified, the report shows `card_source` and any
   `declared_not_armed` modes. A `RuntimeWarning` about
   `'memrack.conformance' found in sys.modules` is a known repo-layout artifact —
   ignorable. pytest alternative: `assert_conformance(obj)`.
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
5. **Layer-split**: the `is_available()`/`get_config_schema()` obligations belong to the
   PROVIDER layer only (memrack's own `__init__.py` carries them against the Hermes
   ABC) — a BACKEND module under backends.d/ does NOT need them. What BACKENDS owe:
   the contract table above + the card."


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

## Worked example

**`contrib/dirsearch.py`** (with its manifest) — a real, guide-only-built backend
(built by a fresh author in ~2 minutes using ONLY this guide + the template;
conformance green on first run). It searches a configurable markdown/text dir
(`provider_config['root']`), honest empty card without a root, UTC-Z ts on
file mtimes, `pointer`-kind hits. Copy it to backends.d/ + manifest to use.

## Pitfalls pre-built in the SDK (reuse, don't relearn)

- fts5 reads `"Hit-me"` as column syntax — always quote tokens: `fts_match_tokens()`.
- JOIN over external-content FTS: qualify EVERY column, also `ORDER BY <table>.rank`.
- SQLite extension load needs `enable_load_extension(True)` before `sqlite_vec.load`.
- A locked method calling a locked method = deadlock — the base uses an RLock.
- `load_memory_provider` fresh-instantiates with skills off unless you ask.
