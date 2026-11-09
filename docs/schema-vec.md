# schema: vec (built-in, opt-in — the split law, 2026-11-08)

- **name**: vec
- **modes**: vec (when the embedder probe passes; the card degrades honestly otherwise)
- **store**: `plugin-data/memrack/vec-index.db` (rowid = fact id; idx_meta: model/dimension/last_seen/legacy_imported)
- **ledger**: memrack.db attached READ-ONLY (harvest + content join); `touch` bumps the shared heat counter (metadata only)
- **config keys (consumed ONLY here)**: embedding_model, ollama_host, vec_backfill_batch
- **membership**: `vec_in_rack` toggle, default OFF — no ollama contact, no cold start while off
- **availability**: probe at init (tags endpoint + model presence); dimension from ONE model probe; mismatch degrades with an honest gap
- **search**: knn + content join; hits carry the fact rowid (heat); `_vec_backlog` honest on every hit when unembedded rows remain
- **legacy**: a pre-split `facts_vec` table inside memrack.db is imported ONCE on first enable (read-only attach) and stays in place, unused
