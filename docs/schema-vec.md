# schema: vec (built-in, opt-in — the split law 2026-11-08, the AUTONOMY law T-047 2026-11-09)

- **name**: vec
- **modes**: vec (when the embedder probe passes; the card degrades honestly otherwise)
- **nature**: an AUTONOMOUS DOCUMENT STORE — the store holds its OWN corpus as
  chunks; the corpus contains ONLY what was ingested here. The automatic ledger
  harvest is ENDED (no auto-embedding of base-db facts, no ledger attach in the
  read path).
- **store**: `plugin-data/memrack/vec-index.db`
  - `vecchunks`  (id INTEGER PRIMARY KEY, source_ref TEXT, chunk_idx INTEGER,
                  content TEXT, hash TEXT, ts TEXT; UNIQUE(source_ref, chunk_idx))
  - `facts_vec2` (vec0 embeddings; rowid = chunk id)
  - `chunk_heat` (chunk_id INTEGER PRIMARY KEY, heat INTEGER) — the OWN
    per-chunk heat counter (doc hits burn here, never the ledger)
  - `idx_meta`   (key, value): model, dimension, schema_version ('2'),
    legacy_imported, chunks_migrated, src_hash:<source_ref> (content-hash
    dedupe per source)
- **ingest**:
  - tool `memrack_vec_add` (args: source_ref, text OR path, chunk_chars=1200,
    chunk_overlap=0, pages) / CLI `hermes memrack vec add --path <file>
    [--ref <ref>] [--pages N]`
  - .md/.txt → text; .pdf → pypdf extraction (OPTIONAL dep: fail loud with an
    install hint when missing; fail loud on empty extraction — NEVER OCR, no
    LLM in the pipeline)
  - mechanical chunking at sentence boundaries when possible, else a hard
    split; batched embeds (`vec_backfill_batch` = the batch)
  - content-hash dedupe: an unchanged source returns `deduped` without new rows
  - RE-ingest of a source_ref = REPLACE (old chunks of the ref out, new in) =
    idempotent refresh
- **search**: knn over ALL chunk embeddings; the content join comes from the
  OWN vecchunks table. Hits carry: backend=vec, kind=doc, chunk_id (+id),
  chunk_idx, source_ref, content, `ref = vec:<source_ref>#c<chunk_idx>`
  (the reading reference for file tools), ts, distance, hits (own counter).
- **heat**: `_touch_facts` routes kind=doc hits to the backend's OWN
  `chunk_heat` counter; kind=fact hits keep burning ledger heat as before.
- **config keys (consumed ONLY here)**: embedding_model, ollama_host,
  vec_backfill_batch (now the ingest embed batch)
- **membership**: `vec_in_rack` toggle, default OFF — no ollama contact, no
  cold start while off
- **availability**: probe at init (tags endpoint + model presence); dimension
  from ONE model probe; mismatch degrades with an honest gap; pypdf missing =
  pdf ingest fails loud with `pip install pypdf` (declared optional in
  plugin.yaml pip_dependencies)
- **legacy migration (one-time)**: pre-T-047 `facts_vec2` harvest rows
  (rowid = ledger fact id) become first-class chunks (source_ref
  `ledger:facts:<id>`); rows whose ledger fact is gone are dropped, counted
  honestly in idx_meta `chunks_migrated`; the legacy ledger tables
  (`facts_vec`) stay in place, unused, documented residue
- **card limits (honest)**: independent corpus — only ingested content is in;
  pdf needs pypdf (optional dep); embedder need + first query after a restart
  = the cold start lives here
