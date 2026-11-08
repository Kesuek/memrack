# wiki — page-search schema (no store of its own)

The `wiki` backend keeps NO database. It wraps an existing markdown wiki
(`~/wiki/main/` by default) and searches whole pages.

## Index model

- **Cache:** `{relative_path: (mtime, size, text)}` in RAM. A `rescan()` pass
  is STAT-FIRST: every page is stat()ed and only a page whose mtime/size
  differs from its index entry is re-read (unchanged pages are never read);
  a deleted page drops out of the index (tombstone, honestly counted). The
  pass returns the number of index changes (new + changed + deleted) and
  the breakdown ({new, changed, deleted, reads}) rides `last_rescan`
  (logged: N pages, M new, M changed, M deleted, M reads).
- **Hits:** `kind=page`, `target = wiki:<relative-path>:L<first-match-line>`
  — the target identifies ONE entry (the RRF collapse law), `snippet` carries
  the matching 3-line window.
- **Scoring (deterministic, stdlib only):** AND-gate over query tokens (one
  missing token kills the page), title match +3.0, body lines containing the
  token +1.0 each; ties break by target path.
- **ts** = refresh time in UTC-Z with microseconds (merge tiebreak law).

## No heat

Pages are not written by memrack (the agent edits files with file tools), so
hits carry no `hits` counter and no heat obligation (pointer-kind vocabulary).
