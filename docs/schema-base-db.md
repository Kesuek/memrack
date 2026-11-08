# base-db — storage schema (intentional)

`memrack.db` under `<hermes_home>/plugin-data/memrack/`, SQLite, 0600 dir.
Created automatically when the provider activates and the backend is enabled
(lazy: an enabled backend creates its tables at `initialize()`).

The schema is **intentional as-is** — one row = one atomic fact.

```sql
CREATE TABLE IF NOT EXISTS facts (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
                      -- fresh stores: a deleted id is never recycled; stores created
                      -- before AUTOINCREMENT keep their schema and rely on the
                      -- facts_vec prune at every delete (the id-reuse fix)
  ts      TEXT NOT NULL,          -- UTC-Z with microseconds (merge tiebreak law)
  kind    TEXT NOT NULL,          -- explicit | curated (legacy input "curation"
                                  -- is tolerated and stored as curated)
  content TEXT NOT NULL,          -- ONE sentence per row (write law)
  target  TEXT,                   -- stable entry identity (RRF collapse law)
  source  TEXT NOT NULL,
  session_id TEXT,
  hash    TEXT NOT NULL,          -- sha256 of content = dedupe idempotence
  hits    INTEGER NOT NULL DEFAULT 0,  -- heat counter (report basis)
  meta    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS fa_hash ON facts(hash);

CREATE TABLE IF NOT EXISTS pointers (
  id       INTEGER PRIMARY KEY,
  subject  TEXT NOT NULL,
  location TEXT NOT NULL,
  kind     TEXT,
  ts       TEXT NOT NULL,
  checked  TEXT                   -- pointer existence-check marker
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    content, content='facts', content_rowid='id');      -- external-content
CREATE VIRTUAL TABLE IF NOT EXISTS pointers_fts USING fts5(
    subject, location, content='pointers', content_rowid='id');
```

Search = FTS5 `MATCH` over the external-content indexes, ranked by
`rank` (SQLite bm25). Advanced `col:term` filter syntax is honored only
when `col` names a real index column; stray colons in plain queries
(time `10:30`, URLs) are tokenized and quoted, never ridden raw.
`touch(ids)` increments `hits` on returned fact rows — one batched
transaction per search, and vec hits carry the fact `id`, so semantic
results burn heat too (one commit per search, never per row).

## Deletes and the optional vec table

Every delete removes by EXACT identity first (dedupe hash OR the per-entry
`builtin:<src>#<hash8>` target); a needle that matches nothing falls back to
a substring sweep and reports what went, how many, and which match won.
Each delete also prunes the optional vector table:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0(embedding float[<dim>]);
```

- `facts_vec.rowid` IS the fact `id` it indexes — pruning at delete keeps a
  recycled id from handing back a stale embedding (works on existing stores).
- `<dim>` comes from a probe of the configured embedding model at
  initialize (never a hard-coded default). An existing table of another
  dimension, or a mismatch discovered at backfill, degrades honestly:
  the engine drops the vec mode with a visible gap note instead of failing
  inserts silently.
- Backfill is batched; the batch is configured via
  `plugins.memrack.vec_backfill_batch` (default 32). Rows still missing a
  vector after a batch are surfaced as `vec_backlog` in the search answer,
  not left as a silent remainder.
