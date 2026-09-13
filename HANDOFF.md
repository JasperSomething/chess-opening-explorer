# Opening Atlas engineering handoff

## Current state

The full Lumbra OTB download and header inventory are complete: 10,355,488 records. The two-pass bulk importer is implemented and a full run has been launched, but **do not claim the local graph is complete until `state.phase` is `complete` and `state.sample` is false**. The UI exposes this distinction. Inspect the live database/log rather than relying on this document for process status.

User selections: use every game in the publisher’s OTB collection, and offer a comparison where both players have numeric ratings >=2200. No extra game-type or date filters. Local source is labelled Lumbra, not Lichess Masters. Original API database is preserved.

## Implemented

- `lumbra.py`: python-chess visitor replays full mainlines; two 128 MiB saturating arrays find candidates without false negatives; exact second pass uses canonical FEN in SQLite. Repetitions count once per game/position with first outgoing continuation. No independent duplicate removal. Invalid games, unknown results and nonstandard starts are excluded and reported.
- Candidate pass checkpoints every 20,000 games, exact pass every 1,000. Input offset and exact counts commit atomically. Candidate flush precedes checkpoint. Replaying a candidate batch only introduces extra candidates. Same settings/input resume; changed input requires new output.
- Exact >=100 all-game edge closure is constructed after counting, with transpositions and incoming links. Both rating groups retain all continuations at retained positions.
- `enrich_lumbra.py`: verified-cache seed via SQLite backup, Lichess-only missing-snapshot queue over the completed local retained graph, serial API rate handling inherited from the existing client. Separate cache avoids mixing this queue with the original Masters crawler.
- UI: all-games and both-2200+ counts side by side; selector changes sorting; each group has its own percentage column. Cached Lichess statistics remain separate. Auto-refresh while importing or missing current Lichess data.
- `explorer.py serve --local-db ...` supports alternate generations. Current local comparison server was launched on port 8766 with `data/lumbra-lichess.sqlite`.

## Verification

17 offline tests pass: legacy API graph tests plus local exact transpositions, Elo boundaries, repetitions, invalid records, canonical hashing, terminal totals, threshold closure, interruption/resume, missing-vs-zero overlay and Lichess-only cache reuse. A real 20,000-game sample completed both passes in ~69 seconds: 20,000 accepted, 1,578 both-2200+, 203 retained positions. This sample is ordered source data, not a representative random sample; do not infer global opening proportions or a precise runtime from it. Browser inspection verified comparison controls, progress and cached Lichess values. Sample databases are never substituted for the full reference.

## Next checks

1. Follow `data/lumbra-import.log`, and read `state` from `data/lumbra.sqlite`. Check free disk and report exclusions. A full run can take many hours; the entire generation has not yet been verified.
2. Once complete, verify root accepted totals, strong subset totals, graph closure, stored move legality and transposed statistics against the source. Confirm no incomplete generation is called complete.
3. The current background pipeline chains import success to Lichess enrichment. Confirm it is still running; after an interruption repeat the commands in README. Lichess coverage will take longer than local reference import.
4. Long-term improvement: profile the exact aggregation and add richer progress/ETA reporting if useful. The disk guard stops below 3 GiB free. No full-corpus disk-size estimate has been proven yet.

## Paths and commands

Source PGN: `data/LumbrasGigaBase_OTB_Complete.pgn`. Local graph: `data/lumbra.sqlite`; preserve its `.candidates` file until complete. API cache: `data/lumbra-lichess.sqlite`. Original `openings.sqlite` stays unchanged by the new pipeline. Some early candidate/sample databases may remain locally for inspection and are ignored by Git.

Follow README for launch, resume, enrichment and rebuild commands. Data, candidate arrays, tokens and logs must stay out of Git. Public source metadata attributes Lumbra’s CC BY-NC-SA 4.0 database. The separate local HERMES-HANDOFF.md includes machine-specific context and must not be published automatically.
