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

- a **rule-based router** classifies what is being stored — by data *type*,
  never by LLM relevance judgement — and writes to the backend that fits it
- retrieval **fans out** across the rack and merges: hybrid retrieval by
  architecture instead of a hybrid monolith
- backends **dock** in two ways: drop a module into
  `$HERMES_HOME/plugins/memrack/backends.d/<name>.py` exporting
  `export_backend()`, or (v0.2+) provide one via the
  `hermes.memrack_backends` entry-point group
- write-side stays **legible**: every router decision lands in
  `$HERMES_HOME/plugin-data/memrack/route_log.jsonl`

## Status — v0.1.0 skeleton

- [x] Provider skeleton (MemoryProvider-ABC compatible, single-slot compliant)
- [x] Backend protocol + registry with `backends.d/` folder discovery
- [x] Rule-based router with route table + JSONL route log
- [x] Built-in `wiki` backend wrapping the wiki-memory provider
      (v0.1 route-all target: identical behavior to running wiki-memory)
- [ ] `fts_store` (SQLite FTS5) backend
- [ ] `vec_store` (sqlite-vec + Ollama embeddings) backend
- [ ] `memory_search(mode=auto|fts|vec|all)` fan-out tool
- [ ] ledger (SQLite WAL, idempotent, replayable)
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
