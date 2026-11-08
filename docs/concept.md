# Concept — why memrack exists

Hermes has **one memory-provider slot**. memrack fills that slot and hosts a **rack**
of dockable cognitive engines — a provider chooses which backend a fact lives in,
and recovery runs across all of them at read time.

## Why not one store for everything?

No single store is uniformly good for all four memory kinds:

- **Wiki (markdown pages)** — explanatory prose, relationships, cross-references,
  curated slowly.
- **Base-db (facts + pointers)** — atomic sentences, exact recall in milliseconds,
  idempotent writes, heat-scored.
- **Vec memory** — semantic recall across large text bodies when the question uses
  different words.
- **Episodic memory** — earlier sessions and events, searchable but never copied
  out of the canonical session history.

## Backend cards — the core idea

Each backend carries a `best_for` / `limits` / `search-modes` card that is shown to
the agent. `best_for` describes what fits; `limits` lists the cases where the
backend is unreliable. The agent uses these cards to **choose a destination at
write time** (deterministic source rules only as fallback — no LLM judge at write)
and to **prioritize backends at read time** — not to call a unified API and hide
the differences.

The tools keep the cards in-flow: `memrack_search`, `memrack_write` and
`memrack_status` each carry the rack cards as an additive `cards` slot in their
answer, rebuilt per call from the live rack — so the agent re-checks the rack at
every tool transition without a separate status round-trip.

Combining stores only works if the backend descriptions are honest and if the merge
preserves both relevance (RRF over per-backend ranking) and provenance (each hit
carries the backend it came from).

## The rule

One format rarely serves structured facts, explanatory knowledge, and semantic
search equally well. memrack keeps one slot but does not flatten the differences —
it makes them usable.
