# base-db — storage schema (intentional)

`memrack.db` under `<hermes_home>/plugin-data/memrack/`, SQLite, 0600 dir.
Created automatically when the provider activates and the backend is enabled
(lazy: an enabled backend creates its tables at `initialize()`).

The schema is **intentional as-is** — one row = one atomic fact.

```sql
CREATE TABLE IF NOT EXISTS facts (
  id      INTEGER PRIMARY KEY,
  ts      TEXT NOT NULL,          -- UTC-Z with microseconds (merge tiebreak law)
  kind    TEXT NOT NULL,          -- explicit | curated
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
`touch(ids)` increments `hits` on returned fact rows.
