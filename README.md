# memrack

One provider slot, many memory backends.

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
  pip entry points come later (v0.3+)
- write-side stays **legible**: every router decision lands in
  `$HERMES_HOME/plugin-data/memrack/route_log.jsonl`

## Status — v0.1.0 skeleton

- [x] Provider skeleton (MemoryProvider-ABC compatible, single-slot compliant)
- [x] Built-in `base-db` backend: SQLite `facts` + `pointers` + FTS5 — stdlib-only,
      solo-functional (the Basisfall: one backend is the whole rack, tested)
- [x] Rack cards (`best_for`/`limits`/`search` — manifest data for docked, attrs for built-ins)
- [x] `memrack_write` / `memrack_search` / `memrack_status` — agent routing,
      fan-out with RRF merge, target collapse, guided empty-hints, honest mode-errors
- [x] Built-in `wiki` backend wrapping wiki-memory (opt-in migration bridge, default parked)
- [ ] vec engine (sqlite-vec + nomic-embed-text, optional upgrade)
- [ ] wiki search engine (in-memory FTS over markdown pages)
- [ ] bridge-adapter-style manifest registry polish (T-003): status surfaces
- [ ] heat report CLI (`memrack report`)
- [ ] catalog submission

## Install (experimental, not yet published)

From source:

```bash
git clone https://github.com/Kesuek/memrack
rsync -a --exclude .git memrack/ $HERMES_HOME/plugins/memrack/
hermes config set memory.provider memrack   # experimental — see below
```

Do not switch productive profiles before v0.2 verification. The plugin is
inactive unless `memory.provider` names it.

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

Write types: `explicit`, `episodic`, `semantic`, `curation`. The router maps
each type to exactly ONE primary backend — no fan-out writes.

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
