# memrack

One provider slot, many memory backends; installed catalog memory providers can
dock into the rack instead of replacing it.

## The problem

In Hermes, memory is a single external provider slot (`memory.provider` in
config.yaml). Every alternative memory system — vector store, keyword
index, curated wiki, cloud service — is a *different provider* with its own
setup, its own extraction pipeline and its own data format. Switching
systems means re-setup, and losing (or migrating) the memory you already
have.

## The idea

`memrack` is ONE MemoryProvider that hosts a **rack of dockable backends**:

- the **agent routes**: each backend carries a card (`best_for` / `limits` /
  `search`), and `memrack_write(content, backend=?)` writes where the agent
  chooses; deterministic source rules are only the fallback. No LLM judge
  anywhere — not at write (an earlier experiment with autonomous LLM routing
  was removed as broken) and not at read (no `auto`-mode for the same reason)
- retrieval **fans out** across the rack and merges: hybrid retrieval by
  architecture instead of a hybrid monolith
- backends **dock bridge-adapter-style** (presence = registered): drop a
  `backends.d/<name>.yaml` manifest (`name`, `module`, `best_for`, `limits`,
  `search`) plus its module; `rm` the manifest = unregistered, a
  `disabled-` prefix = parked. Legacy code-first `.py` drop-ins still work;
  pip entry points come later (v0.5+)
- write-side stays **legible**: every router decision lands in
  `$HERMES_HOME/plugin-data/memrack/route_log.jsonl`
- installed **catalog memory providers can dock into the rack** (`memrack dock
  <provider> [--probe]`): memrack mirrors the provider's ABC — recall fuses into
  ONE prefetch block, the static prompt block concats behind an origin line, the
  provider's tools surface mirrored under memrack-namespace names — the provider
  never talks to the host directly

## Built-in backends

| backend | modes | what it is |
|---|---|---|
| base-db | fts + pointers | the tabular floor: atomic facts, hash-dedupe, FTS5 (stdlib-only, zero network) |
| wiki | fts | whole-page knowledge (opt-in park/unpark) |
| state-episodic | episodic | read-only search over the host session history |
| **vec** (opt-in) | vec (+ `memrack_vec_add` ingest) | AUTONOMOUS DOCUMENT STORE — ingest .md/.txt/pdf into chunks (mechanical, batched embedded) and recall them semantically; own store (`plugin-data/memrack/vec-index.db`: vecchunks + chunk_heat); the corpus holds ONLY what was ingested (the ledger harvest is ended); pdf needs the optional pypdf dep; OFF by default (`vec_in_rack` toggle) — no ollama contact, no cold start while off |

## Status — v0.11.1 (362 tests green)

**Ships with three backends:** `base-db` (the stdlib floor, always on, storage created
at activation), `wiki` (markdown bridge, opt-in), `state-episodic` (READ-ONLY search
over the canonical session history, default on — it copies nothing).

Docs: [`docs/concept.md`](docs/concept.md) (why memrack), [`docs/backend-guide.md`](docs/backend-guide.md) (15-min backend), [`docs/schema-base-db.md`](docs/schema-base-db.md), [`docs/schema-wiki.md`](docs/schema-wiki.md).

- [x] Provider skeleton (MemoryProvider-ABC compatible, single-slot compliant)
- [x] Built-in `base-db` backend: SQLite `facts` + `pointers` + FTS5 — stdlib-only,
      solo-functional (the Basisfall: one backend is the whole rack, tested)
- [x] Rack cards (`best_for`/`limits`/`search` — manifest data for docked, attrs for built-ins)
- [x] `memrack_write` / `memrack_search` / `memrack_status` — agent routing,
      fan-out with RRF merge, target collapse, guided empty-hints, honest mode-errors
- [x] in-flow cards: `memrack_search` carries the rack cards as an additive
      `cards` slot ON A MEMBERSHIP DELTA — the first tool answer of a provider
      instance and every answer after a membership stand change carry the full
      prose (rebuilt per call from the live rack, never a stale init snapshot);
      an unchanged stand answers with the compact marker (`cards_unchanged`,
      the `memrack_status` hint and the per-backend `write_targets` booleans).
      `memrack_write` keeps its full-cards answer; `memrack_status` remains
      the full inspection surface
- [x] rack-status slot: every `memrack_search` answer additionally carries a LEAN
      additive `rack_status` map — per active backend its live `modes` (the same
      one copy the cards are built from) plus `participation` for THIS query
      (`searched` | `skipped` | `gap`), sorted deterministically, no card texts
      (the token law: the card prose stays in `cards`)
- [x] Built-in `wiki` backend wrapping wiki-memory (opt-in migration bridge, default parked) —
      now with a page search engine (`WikiIndex`, stdlib-only: stat-first
      mtime/size pass — unchanged pages are never read —, L<n>-targets, snippets)
- [x] vec engine (sqlite-vec + nomic-embed-text) — OPTIONAL upgrade, since v0.11.0 an
      AUTONOMOUS document store: ingest via `memrack_vec_add` / `hermes memrack vec add`
      (pypdf optional dep, fail loud), mechanical chunking, re-ingest = replace, own
      per-chunk heat; search = knn over the OWN chunk embeddings (ledger harvest ended)
- [x] bridge-adapter-style manifest registry: card-as-data, structural card-vs-code checks,
      2-pass discovery (manifest-referenced modules never double-load), pending-dock rescan
      ("registered (next session)" in `memrack_status`; broken manifests
      surface as docking errors instead — never as a next-session promise)
- [x] backend SDK: `memrack.sdk.MemrackBackend` base (fts5/lock/degrade lessons
      pre-built, OPTIONAL — duck-typing remains the law), `memrack.conformance`
      (`check_backend`/`assert_conformance` + standalone runner), `backends/_template.py`
      + `docs/backend-guide.md` — target: foreign backend 0→docked in <15 min
- [x] state-episodic backend: READ-ONLY search over the canonical session
      history (`state.db`, mode=ro, no copies, no triggers), default ON
- [x] provider CLI: `hermes memrack status|report|export|import` — the heat
      report (hot facts, never-read count) is the curation basis; surfaces only when active
- [x] catalog-provider docking (the `memrack_dock` tool + `hermes memrack dock
      list|show|probe|refresh|remove`): installed, INACTIVE catalog memory providers
      are OFFERED (never auto-docked); `--probe` instantiates the provider class in
      isolation, proves the MemoryProvider-ABC, classifies per provider (tool-centric
      or prefetch-only — the probe outcome is the truth), and writes a DRAFT card +
      per-provider binding shim into `backends.d/` (presence semantics) with probe
      evidence (timestamp + provider version) for the drift recheck (`--refresh`:
      version change → revision note; a REVIEWED card is never overwritten). Mirror
      names follow the 64-cap chain (`memrack__<provider>__<tool>` → `mr__<p>__<t>`
      → `mr__<p>__<t>__<hash4>`, deterministic/restart-stable, collisions fail
      closed) and always sit on the `memrack_status` cards; `best_for`/`limits`
      stay HUMAN-authored (draft placeholders). A card may also record a
      `query_bridge` recipe — the docked provider's search-family tool then
      serves one `memrack_search` rail and its hits join the SAME merged pool
      (the adapter executes the recorded recipe, never a guessed signature;
      see `docs/backend-guide.md` "The card-recorded query bridge")
- [x] uninstall grace for the docks: when a docked provider's install leaves
      the house, its card parks immediately (presence rename, honest
      `uninstalled_at` flag + kept-until note, visibly reported in
      `memrack_status` — never a silent absence, never dock-error spam);
      `dock_retention_days` (default 14, 0 = immediate, -1 = never) decides
      when park turns into removal with a per-file backup, and a re-install
      before the retention restores the card untouched (no re-probe) — and
      the rack keeps the store SWITCHES honest: a parked card's
      `<name>_in_rack` keys flip OFF in both config stores (merge per key,
      atomic 0600 — the gear pane shows no switch "on" for a provider the
      rack no longer hosts), a re-installed card restores with its switch
      still OFF plus a "flip the switch to reactivate" note (the user
      re-enables deliberately), and a parked card whose store switch still
      claims on is reconciled off
- [x] config surfaces: CLI-wizard schema (str-convention) + desktop panel
      (`config_schema.py`, declared surface, flat_json bridge, dual-store save_config)
- [x] config-surface for the docks (T-036): one `<name>_in_rack` membership
      toggle per known backend (built-ins AND docked cards) + the `dock_want`
      select (installed, inactive catalog providers) — both instance-derived
      on the provider's config schema, both write consistently at save time
      (built-ins fold into `enabled_backends`, docked cards park/unpark via
      the card file); a set `dock_want` choice runs the docking machine's
      probe flow at the next initialize (no auto-dock, the user decides)
      and is CONSUMED after it — success or controlled refusal clears the
      select from both stores; the receipt stays visible as `dock_want_last`
- [x] live membership (runtime switching): the four tool paths
      (`memrack_search`/`memrack_status`/`memrack_write`/`memrack_vec_add`)
      re-evaluate the persisted membership stand on EVERY call — a changed
      stand runs the flock-protected fold and rebuilds the rack off warm
      already-initialized instances (park = out of the active rack with the
      instance warm, restore = the cached instance back, no re-probe; a
      never-initialized backend initializes lazily with its honest probe),
      unchanged = one cheap store fingerprint and zero work. Effective on
      the NEXT SEARCH — no `/new` needed; the injected context (prefetch /
      system block) follows the session (the honest channel split)
- [x] pending-confirmation flow: a PROPOSED (draft) dock card carries
      `pending_confirmation: true` + a `pending_hint` on every card surface
      (status + search/write answers); the chat confirm lands via the
      `memrack_card_review` tool (reviewed texts set verbatim, draft cleared,
      one chat-confirm revision, drift-recheck-immun afterwards)
- [x] the declared gear pane carries the family too (T-037,
      import-derived `config_schema.py`): the same `<name>_in_rack`
      toggles + the `dock_want` select render beside the five remaining
      static fields — the toggle switches are the pane's ONLY membership
      UI (the `enabled_backends` dropdown left the pane: a combined-stand
      string could contradict the toggles' individual state; the string
      stays internal store repr the fold maintains); a pane toggle acts
      LIVE — effective on the next memrack search/status/write/vec_add
      call (the runtime membership re-evaluation), the page's schema view
      stays frozen per backend process (the staleness note below)

## Config surface — membership toggles + dock_want (T-036/T-037)

memrack's config reaches the provider over TWO stores that converge in
`save_config` (same block, one write): `plugins.memrack` in `config.yaml`
and the desktop's flat_json `<hermes_home>/memrack/config.json` (0600).
Since T-036 the *provider* surface (`get_config_schema()` — the CLI wizard
`hermes memory setup memrack` AND the dashboard's provider setup modal,
which normalize from exactly this method) is **instance-derived**:

- one `<name>_in_rack` boolean select per known backend, default = the
  current membership stand — for shipped built-ins the stand is
  `enabled_backends` (`_norm_backends` stays the source of truth); for
  docked catalog cards it is presence (`backends.d/<provider>.yaml` exists
  = racked, parked `disabled-<provider>.yaml` = out until re-docked).
- one `dock_want` select listing the installed, INACTIVE catalog memory
  providers (the docking machine's own offer filtering) plus `""` (none).
- one `dock_retention_days` number (default 14): how long an
  uninstalled-parked dock card survives before its card + shim are removed
  (0 = immediately, -1 = never; each removed file lands as a backup under
  `plugin-data/memrack/removed-cards/`).
- two fan-out limits beside it: `fanout_timeout_s` (default 5) — the
  per-backend hook budget; a hanging engine answers an honest gap, the rack
  keeps delivering — and `prefetch_max_chars` (default 4000), the total
  character budget of the merged prefetch block (longer merges truncate
  with an honest `[+N chars truncated]` note).

Everything writes consistently at save time: toggling a built-in folds into
`enabled_backends` (deduplicate, `base-db` as the floor is never fully
toggled off, wiki stays parked-by-default); toggling a docked card
parks/restores its card FILE under the `disabled-` prefix. A set `dock_want`
choice runs the docking machine's probe flow (isolated probe → draft card +
binding shim) at the NEXT `initialize()` — an explicit user decision, no
auto-dock. The choice is CONSUMED in that initialize (one-shot receipt): a
success OR a controlled refusal clears the select from both stores (0600,
atomic) and the live config, so no later initialize re-runs the same probe;
the outcome stays visible as `dock_want_last` {provider, outcome, at}
(`memrack_status` carries it additively).

The DECLARED desktop panel (gear → Plugins → memrack) carries the SAME
family (import-derived `config_schema.py`): the five remaining static
fields — force-backend override, auto-enable toggle, top_k, the vec
group — stay first, then one `<name>_in_rack` toggle per known
backend (the shipped built-ins plus the ACTIVE dock cards in
`backends.d/`; the default follows the current membership stand) and the
`dock_want` select. The pane renders NO `enabled_backends` dropdown:
the toggles are its only membership UI, while the comma string remains
the internal store representation the initialize fold keeps consistent
(dedupe, the `base-db` floor). The setup wizard keeps the select for the
first-setup flow. Both panes write the same dual-store block, so the
stores stay the single source of truth.

**Staleness (Achtung)**: the web server caches the schema per file path,
in-process — the family (docked names, the `dock_want` offers, the toggle
defaults) is FROZEN per backend process: a backend docked LATER appears
in the pane only after a backend restart. The memrack membership itself
waits for NO restart and needs NO `/new`: a pane toggle is effective on the
NEXT SEARCH — true runtime switching. The four tool paths
(`memrack_search`/`memrack_status`/`memrack_write`/`memrack_vec_add`)
re-evaluate the membership stand per call (a cheap store fingerprint; a
changed stand runs the flock-protected fold and rebuilds the rack, reusing
warm backend instances), the persisted key parks/restores the docked card
file or folds into `enabled_backends` exactly as before. The channel split,
honestly: searches see the rack live; the injected context (the prefetch /
system block of the session-context pipeline) follows the session — it
does not re-evaluate per call. Only the pane's schema surface lags, never
the rack.

## Install (experimental, not yet published)

From source (copies ship without `tests/` and `docs/` — the same exclude list
as `scripts/deploy_profiles.sh`, which is the canonical multi-profile deployer):

```bash
git clone https://github.com/Kesuek/memrack
rsync -a --exclude .git --exclude tests --exclude docs --exclude .pytest_cache \
  --exclude __pycache__ memrack/ $HERMES_HOME/plugins/memrack/
hermes config set memory.provider memrack   # experimental — see below
```

Do not switch a productive profile before the test-profile activation run has
passed. The plugin is inactive unless `memory.provider` names it.

## The rack contract (for backend authors)

A backend is any module exposing `export_backend()` returning an object with:

| member | meaning |
|--------|---------|
| `name` | unique backend id |
| `write_types` | tuple of write-type strings it accepts |
| `initialize(session_id=, hermes_home=, **kw)` | called once per session |
| `on_turn(user, assistant, session_id=)` | episodic turn storage |
| `on_memory_write(action, target, content, metadata=, session_id=)` | explicit facts |
| `on_session_end(messages, session_id=)` | session-end extraction |
| `prefetch(query, session_id=) -> str` | per-turn context injection |
| `system_prompt_block() -> str` | static prompt contribution |
| `on_pre_compress(messages)` | pre-compression insight rescue (optional) |
| `shutdown()` | close resources |
| `export() -> dict` | inventory, shown by `memrack_status` |
| `search(query, mode, k) -> list[Hit]` | *optional* — the rack merges (RRF); card must claim the modes it ships |
| `write(kind, content, tags=, session_id=)` | *optional* — atomic fact store; without it `memrack_write` answers a guided error |
| `touch(ids)` | *optional* — heat counter (hits++), the report basis |

Write kinds: `explicit`, `curated` — that is ALL memrack stores atomically. `episodic`
stays Hermes-native (state.db is canonical); `semantic` is a search MODE (the vec
engine), never a write kind. The AGENT chooses the destination per write
(`backend=?` in `memrack_write`); kinds only drive the deterministic fallback.
A write goes to exactly ONE backend — no fan-out writes.

Provider hooks (`prefetch`, `system_prompt_block`, `on_session_end`,
`on_pre_compress`, `on_session_switch`) DO fan out to every active backend
(decision (a), T-031): a backend without the hook is silently skipped, one
engine's failure stays isolated (warn log; per-backend `engine_errors` inside
the packed multi-result dict), prompt blocks merge non-empty + dedupe with
blank-line joins, and a single result keeps the exact pre-fanout form.
The fan-out runs in PARALLEL since v0.8.0: every member answers in its own
worker thread behind a per-backend budget — `fanout_timeout_s` (default 5 s;
the desktop pane declares it). A hanging engine becomes an honest gap the
moment its budget runs out (`engine_errors` naming the timeout) and the rest
of the rack still delivers; results are collected deterministically in rack
order, never in completion order. The merged `prefetch` block carries a
total character budget — `prefetch_max_chars` (default 4000): longer merges
truncate at the budget with an honest `[+N chars truncated]` note (stable
rack order, the note rides the block).

One data channel is gated per card: a docked member receives the
`on_session_end` TRANSCRIPT only when its card records the human-confirmed
`forward_transcript: true` decision (default off, same family as
`forward_writes`/`forward_turns`). Without the recorded decision a dock
still receives the session-end EVENT — with the transcript payload set to
none — while code-first built-ins keep the transcript byte-identical (the
same object, exactly as before). The gate rides the `memrack_status` card.
Memory-MIRROR writes (the host memory tool's mirror into the rack) are
suppressed for the automatic contexts (`agent_context` cron/subagent, per
the ABC's initialize contract) with an honest route-log note; explicit
`memrack_write` tool calls stay the agent's decision whatever the context.

## A search answer (the rack-status slot, cards on delta)

`memrack_search` merges the fan-out (RRF) and answers with results, engine
errors, the cards (on a membership delta) AND the lean rack-status slot — per
active backend its live modes and this query's participation (sorted
deterministic order, no card prose):

```json
{
  "results": [{"backend": "base-db", "kind": "fact", "content": "…", "rank": 1}],
  "engine_errors": [],
  "rack_status": {
    "base-db":       {"modes": ["fts", "pointers"],  "participation": "searched"},
    "state-episodic": {"modes": ["episodic"],        "participation": "searched"},
    "wiki":          {"modes": [],                  "participation": "skipped",
                      "reason": "mode 'fts' not carried"}
  },
  "cards": {
    "cards_unchanged": true,
    "hint": "memrack_status for the card prose",
    "write_targets": {"base-db": true, "state-episodic": false, "wiki": true}
  }
}
```

A backend that could not run marks itself honestly (`skipped` with a short
reason, or `gap` = its entry sits in `engine_errors`); the zero-hit and the
error answers carry the same slot. **Cards on delta, status always:** the
FULL card prose rides only on the first tool answer of a provider instance
and after a membership stand change; an unchanged stand answers with the
compact marker above (the `memrack_status` hint plus the per-backend
`write_targets` booleans — the write vocabulary stays known without prose).
`rack_status` stays on EVERY answer.

## Design principles

1. **No autonomous LLM routing at write time.** An earlier experiment with
   LLM-based nightly routing ("Dreaming") was removed as broken;
   memrack keeps routing mechanical.
2. **Everything is stored.** Relevance judgement belongs to retrieval and
   curation, not to ingestion.
3. **Embedded only (for now).** SQLite/FTS5/sqlite-vec at this scale beats
   running a database server. Server backends can dock later — that is
   what the rack is for.

## License

MIT

## Ships with a dev skill

`skills/memory-provider-dev/SKILL.md` — building, auditing and docking memory
providers: the ABC conformance check, the three config surfaces, the fail-closed
docking contract, mirrored-tool naming, and the fixes for the pitfalls this
plugin's growth uncovered. See `docs/backend-guide.md` for the reference flows.
