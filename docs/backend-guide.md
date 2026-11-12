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
re-derives from a fresh probe on a provider version change instead. The
draft card carries ONE honest boundary line (`dock_boundary`): turn
ingestion is opt-in (`forward_turns` — nothing sent to the dock unless
gated), the prefetch rail is the rack's one fusion fan-out (automatic,
availability-gated — NOT an opt-in), and the tool mirror + system block
follow the card's recorded decisions. The session-end TRANSCRIPT is the
card-gated channel beside them: nothing transcript-shaped reaches a dock
unless the card records `forward_transcript: true` (human-confirmed,
default fail-closed; the draft card's `dock_boundary` line names it, and
the gate rides the `memrack_status` card). Draft cards also carry ONE
honest checkpoint line — `checkpoint: not forwarded
(pre_compress_checkpoint_api_version is not set by the rack)` — because
memrack sets no checkpoint API version: a docked provider's fail-closed
checkpoint guarantee cannot be claimed through memrack.

### The hook fan-out is parallel and budgeted

Every hook fan-out runs per-backend in parallel (`spawn_context_thread`
under the host, a plain daemon thread hermes-less) behind a per-backend
timeout — `fanout_timeout_s` (default 5 s): a hanging engine becomes an
honest `engine_errors` gap naming the timeout, and the rack still delivers
in deterministic rack order (never completion order). The merged prefetch
block carries a total budget — `prefetch_max_chars` (default 4000) that
truncates honestly with a `[+N chars truncated]` note. Keep hooks CHEAP:
both limits exist for the slow/external engine you did not measure.

### The initialize side-effect block is one lock

The fold (persisted membership toggles), the uninstall grace and the
dock_want consumption all park/restore card files and rewrite BOTH config
stores — parallel runs on one home (cron, gateway, a second chat, the CLI)
serialize behind one `fcntl.flock` on `plugin-data/memrack/init.lock`
(generous 10 s budget, small retry polls; plain member reads stay
unlocked). A
home still busy after the budget skips its side effects for THAT
initialize with an honest note (every effect is idempotent and re-runs at
the next one); a concurrent initialize can never consume the same
`dock_want` twice — the one-shot receipt in the store stand is the
serialized runner's proof that the work is done. The per-call membership
re-evaluation (the runtime-switch law) runs the fold under this same
lock from the four tool paths; a busy lock is an honest skip note and
the stand applies unchanged at the next tool call.

### The dock_want select is consumed (one-shot receipt)

A choice set on the `dock_want` select (gear pane or wizard) runs the
machine's probe flow at the NEXT `initialize()` — and then the select ENDS:
a success OR a controlled refusal (unknown/failed provider, dock-name
refusal, parked card) clears `dock_want` from BOTH stores
(`plugins.memrack` in `config.yaml` + the flat json `<hermes_home>/memrack/
config.json` — merge per key, atomic tmp sibling, 0600, the host config
never clobbered) and from the live config, so no later initialize re-runs
the same probe over a stale select. The outcome stays visible as
`dock_want_last` {provider, outcome, at} in both stores — `memrack_status`
carries it additively (an absent receipt stays absent). A failed store
write is fail-closed: the files still carry the choice, so it persists for
an honest retry and the receipt names the write error.

### The rack_status slot (every search answers the rack)

Every `memrack_search` answer besides the cards carries a LEAN additive
`rack_status` map — per ACTIVE backend its live `modes` (read from the SAME
one-card source the search routes with: one copy, no second truth) plus
`participation` for THIS query:

```json
"rack_status": {
  "base-db": {"modes": ["fts", "pointers"], "participation": "searched"},
  "vec":     {"modes": ["vec"],             "participation": "skipped",
              "reason": "mode 'fts' not carried"},
  "wiki":    {"modes": ["fts"],             "participation": "gap"}
}
```

`searched` = the backend's fan-out ran; `skipped` carries a short `reason`
(mode not carried, none live, or the request rejected before fan-out) and
`gap` maps to that backend's `engine_errors` entries. Keys are sorted
(deterministic), the slot shows the WHOLE rack even on a targeted search
(status = the state question, routing = the query's business), and it never
carries card prose (`best_for`/`limits` stay in `cards` — the token law: the
slot costs a flat handful of tokens per active backend). The slot rides every
answer path, including zero-hit and honest-error searches.

### Cards on delta, status always

The FULL card prose leaves the provider only on a membership delta: the first
tool answer of a provider instance carries the full `cards` map, and every
answer after a membership stand change does too (the same stand hash the
live-membership law uses — one source, no second truth). An unchanged stand
answers with the compact marker, so repeated searches stop paying the prose
twice:

```json
"cards": {
  "cards_unchanged": true,
  "hint": "memrack_status for the card prose",
  "write_targets": {"base-db": true}
}
```

`write_targets` = one line per active backend, `true` when it carries an
atomic write (the write vocabulary never goes unknown). Write and vec_add
calls keep their own answer form and advance the delta memo — the next search
stays consistent. The full prose is always available on demand:
`memrack_status` remains the complete inspection surface (cards, gaps, parked
docks, switch receipts).

### The CLI pipeline and the fresh init report the same card

`hermes memrack status` (the inventory pipeline) and a fresh `initialize()`
on the SAME home build their cards from the same on-call card reconstruction —
pinned by test (the suite runs both and compares). A backend whose environment
probe fails degrades identically in both: its card ships empty `modes` WITH the
honest `env_gap` reason (never a silent promise in one pipeline and silence in
the other), and the optional semantic backend's package requirement is declared
in the manifest (`pip_dependencies`) so the environment provisioning carries it.

### The dock-name law (every name-built path, one validator)

Card/provider names are lowercase dashes, underscores and digits —
`^[a-z0-9][a-z0-9_-]{0,63}$` (the structural `disabled-` park prefix is not
part of the budget). `dock`, `dock show`, `dock remove`, the card review
(`memrack_card_review`), the rescan's pending report and the `dock_want`
select all build their paths behind ONE central validator + containment:
a name outside the law is a controlled, honest refusal (no dots, so no
`..` and no traversal by construction), and any path that resolves()
OUTSIDE `backends.d/` — e.g. a symlink planted at a card name — is refused
the same way. A card's `module:` field is honoured on remove only under the
same gates: the shim candidate must resolve inside the dock dir.

### Remove deletes the card AND its machine-generated shim

`memrack dock remove <provider>` (or the tool's action=remove) removes the
active card, the parked `disabled-<provider>.yaml` variant AND the binding
shim beside the card — but ONLY shim files the MACHINE generated (the
provenance header "Auto-generated by the memrack docking machine",
marker-checked). A user-owned legacy module at the same `<provider>.py`
side name STAYS (the legacy contract: plain code-first drop-ins keep
loading), and discovery never resurrects an ORPHAN: a marker-carrying shim
whose referencing card is gone is reported honestly and skips the legacy
load (removed providers do not return next session).

### The uninstall grace (park first, remove after retention)

A docked provider whose install leaves the house does NOT vanish silently.
At every `initialize()` — and on every `dock <provider> --refresh` — the
rack checks the install against the card; when the provider is neither a
directory, a pip entry point nor loadable anymore, the card PARKS
immediately (the established `disabled-<provider>.yaml` presence rename,
shim untouched) and carries `uninstalled_at` + `uninstall_note`
("provider uninstalled — parked; card kept until ..."). The parked card
stays visible: `memrack_status` lists it under `uninstalled_docks` with
the remaining retention days, while the rack simply routes around it (no
gap noise, no dock-error spam). A provider that is merely INACTIVE (its
install directory still exists — the plugins hub disabled case) is NOT an
uninstall: the existing park/gap laws keep applying and no flag is set.

The clock is `dock_retention_days` (default 14; 0 = immediate; -1 =
never): after the retention expires, the card and its shim are REMOVED —
each file copied first to `plugin-data/memrack/removed-cards/` (0600) as
an honest backup, reported as "removed card <name> after retention". A
re-install before the retention restores the card as it was (rename back,
flag removed, no re-probe — recorded card decisions survive), and a card
parked WITHOUT the flag (the membership toggle's explicit decision) is
never touched by the grace. Hand-written bridge manifests (name/module
only, no machine dock evidence) live outside this law entirely.

**The switch-consistency half.** The `<name>_in_rack` toggles are the
config surfaces' ONLY membership keys, so the law never leaves one lying
against the rack: when a card parks, `memrack` flips the key to `false`
in BOTH stores (`plugins.memrack` in `config.yaml` and the flat json
`<hermes_home>/memrack/config.json` — merge per key, atomic tmp sibling,
0600, the host config never clobbered: a store that does not parse is
skipped with an error, never "repaired"; a store already at the target is
not rewritten). When the Hermes tree is importable in the running process,
the write rides the HOST's own config helpers (`save_config` for the yaml
block, `atomic_json_write` for the sidecar) with the guarded in-plugin
writers as the hermes-less fallback — the merge-per-key / atomic / 0600
contract stands in both modes. A re-install before the retention restores the card as
recorded, but the switch STAYS OFF — the store keeps `false` and the
restore receipt carries "the membership switch stayed OFF — flip the
switch to reactivate" (only when the switch reads off after the write; a
switch the user deliberately left ON keeps the plain presence restore).
Until the user flips the switch, the initialize fold keeps the restored
card parked. A parked card whose store switch still claims on (pre-law
stands) is reconciled off at the next initialize or `dock --refresh`, the
card file itself untouched. The `enabled_backends` string is never
written by this law (internal store repr, maintained by the fold); hermes-
less (no provider hub) the whole law stays inert, store writes included.
`memrack_status` serves the receipts: `restored_docks` (with the hint)
and `switch_consistency` (every store switch the law flipped).

### The gear pane (declared surface) and its staleness

The desktop gear pane (Plugins → memrack) renders the membership family
from `config_schema.py`: the module computes `CONFIG_SCHEMA` AT IMPORT
from the same config-surface seams the provider uses — one
`<name>_in_rack` bool per backend (shipped built-ins; your dock card racks
by presence the moment its file exists) ahead of and beside the static
base fields, plus the `dock_want` select. The switches are the pane's
only membership UI: the `enabled_backends` dropdown left the pane (its
comma string stays the internal store repr, maintained by the fold, and
the setup wizard keeps its select), so a combined-stand string can no
longer contradict the toggles' individual state. The web server caches
that schema per process: a dock written AFTER the backend started shows
up in the pane only after a backend restart. The membership itself never
waits for one and needs no `/new`: a pane toggle is effective on the NEXT
SEARCH — the four tool paths (`memrack_search`/`memrack_status`/
`memrack_write`/`memrack_vec_add`) re-evaluate the persisted membership
stand per call (one cheap store fingerprint; a changed stand runs the
SAME flock-protected fold an initialize runs and rebuilds the rack,
reusing warm already-initialized backend instances — park = out of the
active rack with the instance kept warm, restore = the cached instance
back, a never-initialized backend initializes lazily with its honest
probe). The persisted key parks/restores the card file or folds into
`enabled_backends` exactly as before. THE CHANNEL SPLIT, honestly:
searches see the rack live; the injected context (prefetch and system
block, the session-context pipeline) FOLLOWS THE SESSION — it does not
re-evaluate per call. Only the pane's schema surface lags, never the
rack.

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
   loads suffix per entry (hash8). Targetless hits fall back to a
   `backend:content` key: identical content out of DIFFERENT backends stays
   separate pool entries (the backend identity never collapses).
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
- **The store switch mirrors reality** — the membership toggles are the config
  stores' only membership keys: the grace law flips them off when it parks a
  card and keeps them off when it restores one; a store that claims a member
  the rack no longer hosts is reconciled, never trusted.
- **All storage under `<hermes_home>/plugin-data/memrack/`** (profile-scoped, 0600-ish
  private dirs) — never `$HERMES_HOME/cache/` (scratch prunes!), never hardcoded homes.
- **EPISODIC turns are NOT copied** — Hermes state.db is canonical; only facts/pointers
  go into backends. The ONE documented exception is the per-card turn gate: a
  docked provider whose card records the human-confirmed `forward_turns: true`
  receives raw turn events through an isolated per-backend fan-out (a failing
  dock is logged, the routed receipt stays THE answer) — absent key = nothing,
  exactly as before. The gate rides the `memrack_status` card.
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


## The vec backend (the autonomous document store)

base-db is the tabular floor: facts, pointers, FTS5 — stdlib-only, no network,
no embedding calls, no cold start. The built-in `vec` backend (opt-in over the
`vec_in_rack` toggle) is an AUTONOMOUS DOCUMENT STORE (T-047, 2026-11-09):

- own store: `plugin-data/memrack/vec-index.db` — `vecchunks` (id, source_ref,
  chunk_idx, content, hash, ts UNIQUE per ref/idx) holds the document chunks,
  `facts_vec2` carries the embeddings (rowid = chunk id), `chunk_heat` the OWN
  per-chunk counter; idx_meta carries model/dimension/schema_version and the
  per-source content hashes (`src_hash:<ref>`).
- the automatic ledger harvest is ENDED: the corpus holds ONLY what was
  ingested (the ONE remaining read-only attach is the one-time migration of
  the legacy harvest rows into first-class chunks, source_ref
  `ledger:facts:<id>`).
- ingest: `memrack_vec_add` (tool) / `hermes memrack vec add --path <file>
  [--ref <ref>] [--pages N]` — .md/.txt to text, .pdf via pypdf (OPTIONAL dep,
  fail loud with an install hint when missing; fail loud on empty
  extraction, this backend never runs OCR); mechanical chunking (sentence
  boundaries when possible, otherwise a hard split; `chunk_chars` default
  1200, `chunk_overlap` optional), batched embeds (`vec_backfill_batch` is
  the embed batch); re-ingest of a source_ref REPLACES (idempotent refresh);
  content-hash dedupe skips an unchanged source. No LLM calls — the
  embeddings are the only model contact.
- ingest path CONTAINMENT: file paths resolve (symlinks included) and must sit
  under a configured ingest root — `vec_ingest_roots` (comma-separated; the
  fallback is the home dir plus wiki_root when configured). The SECRET WALLS
  refuse FIRST and win over any root: everything inside HERMES_HOME or
  ~/.ssh, and every `.env*` path, is a PermissionError before the file is
  touched — a prompt-injected path cannot ship secrets to the embedding host.
  Widen the roots in the memrack config (provider surface field, vec group).
- search: knn over ALL chunk embeddings, the content join comes from the OWN
  vecchunks table (no ledger dependency); hits carry `kind=doc`, `chunk_id`,
  `source_ref`, a reading reference `ref = vec:<source_ref>#c<chunk_idx>`,
  ts, distance, and the per-chunk heat counter. Doc hits burn the OWN
  counter — the ledger heat is a base-db matter only.
- availability: embedder probe at init (ollama: tags + model presence; openai
  style: `/models` with the configured key — the model ID itself is not gated,
  the first embed call fails loud instead); without a live embedder the card
  degrades honestly (no vec promise) and the rack
  keeps answering fts/pointers/episodic — the probe's reason rides the card
  additively as `env_gap` (fresh-init cards AND the served status, one copy).
- embedder routing (NO hard wiring): `embedding_endpoint` `ollama` (default,
  unchanged local behavior) or `openai` — any OpenAI-compatible embeddings
  endpoint (`embedding_base_url` = the base_url of a provider already
  configured in Hermes, e.g. the Nous Portal inference API, a self-hosted
  llama.cpp `/v1`; `embedding_api_key_env` = the NAME of the env var holding
  the key, resolved through the Hermes secret scope first). Keys never live in
  config.yaml. A different embedding model/dimension needs the vec store
  re-ingested (`idx_meta` carries model + dimension — no live re-index).
- limits: an INDEPENDENT corpus — only what was ingested is in; pdf needs
  pypdf; the cold start after a restart lives here (the model load), warm
  queries measure in the hundreds of ms.
- legacy: a `facts_vec` table left inside memrack.db from the pre-split era
  was imported ONE TIME on first enable and — since T-047 — migrated into
  first-class chunks; the legacy tables stay in place, unused, documented
  residue.
