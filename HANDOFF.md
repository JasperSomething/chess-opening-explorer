# Opening Atlas engineering handoff

## Current state

Every import is finished. Do not repeat the earlier claim that the pipeline is still
running, and do not re-run a completed stage.

| Database | State |
|---|---|
| `data/lumbra-2200-complete.sqlite` | complete. 10,355,488 scanned, 2,823,189 accepted, every 2200+ position, threshold 1 |
| `data/lumbra-2200.sqlite` | complete. 2,823,189 accepted, frequent positions at threshold 100, 46,044 positions |
| `data/lumbra.sqlite` | complete. All 10,355,488 games accepted, threshold 100 |

User selections stand: every game in the publisher's OTB collection, plus a comparison
where both players have numeric ratings >= 2200. No game-type or date filter. Local source
is labelled Lumbra, not Lichess Masters.

There are two apps and they answer different questions.

## App 1: position browser (`explorer.py`, port 8766)

All-games and both-2200+ counts side by side, each with its own percentage column;
selector changes sorting and move ordering. Lichess statistics are separate and may
remain pending without affecting local counts. Follow README for launch and resume.

## App 2: opening mainlines (`study/mainlines_server.py`, port 8790)

Read-only. Serves `mainlines_ui/` and `analysis/mainlines.json`, writes nothing.

```sh
python3 scripts/build_mainlines.py        # builds analysis/mainlines.json (~14 s)
python3 study/mainlines_server.py --port 8790
```

The dataset joins the 2200+ frequent-position index with the lichess-org/chess-openings
taxonomy, which the builder downloads into `data/taxonomy/` on first run:

- each named line is extended by the most-played 2200+ continuation (min 100 games per
  step, to ply 22)
- results are grouped by FINAL POSITION, not by name, so move orders that transpose
  collapse into one entry. This grouping is forced by the index, which keys on canonical
  position identity and therefore pools transpositions by construction: move-order-only
  distinctions cannot be recovered from it, no matter how the code is written
- the representative name comes from an ordered preference list in the builder
  (structure-defining names outrank move-order umbrellas), falling back to member count
- each entry carries every ECO code and every alias name in its group; aliases are
  searchable. Nothing is discarded by the condensation
- each entry gets its colour-complex counterpart: the position mirrored (piece colours and
  ranks swapped, side to move and castling swapped, en-passant file mirrored) and looked
  up on board + castling. The side to move is ignored on purpose, because swapping colours
  always flips it, so a twin is one tempo away and never sits at the same ply. Only mutual
  pairs are kept (A pairs with B only if B mirrors back to A)

Current output: 1,259 entries from 2,256 named lines, 93 families, 191 entries carrying
more than one ECO code, 114 entries in mutual colour-twin pairs. The King's Indian
position carries 20 codes (A04–A48, E60–E98) across 32 collapsed lines.

Every number in the app is the count of 2200+ games reaching that position. There is no
engine evaluation and no speed or rating stratification.

### Two bugs this pipeline had, both silent

Recorded because both were invisible from the outside and both are easy to reintroduce:

1. The taxonomy line's endpoint was read AFTER the extension advanced the board, so the
   "was this named line ever played at 2200+" test was really measuring the end of the
   derived line. That excluded 1.a3 and roughly 790 other real named lines. The endpoint
   must be captured before extending.
2. Each entry's volume was the sum of its members' endpoint counts. Members share one
   final position by construction, so this added up unrelated positions and reported
   1,006,388 games for a position 2,858 games reach. It is now the position's own reach.

## Verification

286 offline tests pass, 14 skipped. `tests/test_mainlines_app.py` is the app's own suite
and asserts the invariants that matter: the mirror is an involution; it swaps colour, turn
and castling and moves the en-passant file; replaying mirrored moves from the mirrored
start lands on the mirrored endpoint; a twin never keeps the side to move; every stored
line replays legally; every stored FEN is the position after its move, not before; no two
entries share a position; twins are mutual and genuinely mirrored; families partition the
entries.

The browser UI was checked by loading it, not by assuming: the board renders, the move
list steps, filters and search work, and the twin jump navigates.

## Open work

1. **The user's own definition of "mainline" is not yet written down.** The app is a first
   attempt built to be corrected: frequency mainlines, not theoretical ones. The user said
   they know what they consider mainline and would write guidelines. Expect the variant
   list and the choice of definition to change. The representative-name preference list is
   one edit in the builder.
2. **Variant granularity is unresolved.** Three defensible options: the 3,815 taxonomy
   names, the 1,259 condensed positions, or an explicit user-authored list.
3. **Dead code.** The Scandinavian study library (39 modules besides `__init__.py` and the
   new `mainlines_server.py`: `curriculum_build/audit/compare`, `course_report`,
   `course_walkthrough`, `plans`, `family`, and the rest) is still present. The course app
   it fed has been removed, and four of those modules read `analysis/course.json`, which no
   longer exists. Removing them is pending a decision.
4. **Counterpart coverage is bounded** by the taxonomy: a genuine left-handed line nobody
   has named will not be found.
5. The Lichess enrichment queue over the all-games retained graph has not been confirmed
   complete. Check `data/lumbra-enrichment.log` and the cache rather than assuming.

## Paths and commands

Source PGN: `data/LumbrasGigaBase_OTB_Complete.pgn`. Databases and `analysis/` are both
ignored by Git: a clone has the code and the builders, not the data. The mainlines dataset
regenerates in about 14 seconds from the 2200+ index.

Follow README for launch, resume, enrichment and rebuild commands. Data, candidate arrays,
tokens and logs must stay out of Git. Public source metadata attributes Lumbra's
CC BY-NC-SA 4.0 database. The machine-specific `HERMES-HANDOFF.md` under `outputs/` holds
local process state, paths and credentials and must not be published.
