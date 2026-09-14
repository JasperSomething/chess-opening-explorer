# Opening Atlas — study-priority analysis layer (design)

Scope: compress opening theory into the small number of positions, structures,
deviations and principles worth a human's study time. First experimental domain:
**White versus the Scandinavian Defence, 1.e4 d5**.

This document is the required pre-run design. It describes the analysis database,
every formula, the perspective conventions, what happens when a dataset is
missing a move, the sample-size guards, reach probability in a transposition
graph, attractor detection, and the engine budget. The code in `study/`
implements exactly these definitions; `tests/test_study_metrics.py`,
`tests/test_study_structures.py` and `tests/test_study_flow.py` cover the maths.

Guiding rule from the research brief: **never equate disagreement between
masters and Lichess with importance.** Distribution differences are recorded as a
descriptive signal (Jensen-Shannon) and never feed the ranking. Study value comes
from *expected engine loss*, i.e. what a population actually gives away by
playing the moves it plays.

---

## 0. Where the data comes from and what "unknown" means

| source | database | coverage recorded as |
|---|---|---|
| `local2200` | `data/lumbra-2200.sqlite` (frequent-position 2200+ index, threshold 100, complete) | `complete` |
| `local2200all` | same table's all-games columns, same retained positions | `complete` |
| `lichess` | `data/lumbra-lichess.sqlite`, `source='lichess'` (aggregated Opening Explorer) | `partial` |
| `masters` | same cache, `source='masters'` | `partial` |
| `local2200complete` | `data/lumbra-2200-complete.sqlite` (every 2200+ position; **importing**, was ~34% at design time) | `partial` |
| `allgames` | `data/lumbra.sqlite` (all games, threshold 100; **importing**) | `partial` |

Every `position_source` row carries `coverage_state ∈ {complete, partial, absent,
zero}` plus the source's own games at the position, its domain-entry games, and
the ratio between them. A position missing from a `complete` source is `zero`
(genuinely no qualifying games); missing from a `partial` source is `absent`
(unknown). "No data" is therefore never rendered as zero games.

The analysis layer only ever opens ingestion databases read-only, and writes only
to `data/atlas-analysis.sqlite`.

---

## 1. Analysis database schema

Full DDL: `study/schema.sql`. Tables and their purpose:

| table | contents |
|---|---|
| `meta`, `analysis_run` | provenance: domain definition, per-run config JSON, start/finish times |
| `position` | domain positions: 34-byte key, 4-field and full FEN, ply, min ply, role (side to move), structure id, pawn bitboards (hex), 12 piece bitboards (JSON), castling state, development counts/status |
| `position_source` | per source: games, W/D/L, entry games, count ratio, coverage state |
| `move_source` | per source per move: games, W/D/L, share (denominator excludes terminal games) |
| `provenance` | derived **reverse edges**: `(child, parent, move, source, games)`, indexed both ways |
| `structure` | exact pawn structure + all structural indicators |
| `position_flow` | reachability: flow reach, count ratio, leakage, late-arrival mass, contributing/total parent counts, path count |
| `structure_stat` | per structure: positions, incoming edges, parents, coverage bounds, persistence, development signatures, attractor score + components |
| `eval` | engine evaluation cache: fen, multipv, source, depth, engine, nodes, POV, PVs, meets-threshold |
| `metric` | per position: every regret/gap/JS/reach component plus the products |
| `deviation` | per opponent move: probability, concession, class, engine best response, priority |

Two rules are structural, not advisory:

* **Components are persisted individually.** `metric` stores
  `regret_lichess_*`, `regret_expert_*`, `gap_*`, `js_divergence`, `reach_*`,
  `eval_coverage`, sample sizes and confidence *alongside* the product; nothing
  stores only a final score. Same for `deviation`, `structure_stat`,
  `position_flow`.
* **Cache reuse.** `eval` is keyed by `(fen, multipv, source, depth)` so a
  position is analysed once and re-runs cost no engine time.

---

## 2. Formulas and normalisations

Common currency: **expected points (EP)**, from the point of view of a named
player, in `[0, 1]`.

```
EP_wdl(w, d, l) = (w + d/2) / (w + d + l)        # preferred
EP_cp(cp)       = 1 / (1 + 10^(-cp/400))         # fallback when only centipawns are known
EP_mate(mate)   = 1.0 if mate > 0 else 0.0       # forced mate for the side to move
```

Distribution at a position for a source, with `M` the moves played there and
`n_s(m)` the games in which a game played `m` there:

```
N_s            = Σ_{m ∈ M} n_s(m)      # terminal games excluded (move = 0)
π_s(m)         = n_s(m) / N_s
```

Regret of a distribution against a fixed evaluation set `E` (same EP method for
every move, so the numbers are comparable):

```
Regret(P, π, E) = Σ_{m ∈ E} π'(m) · max(0, EP_best − EP(m))
EP_best         = max_{m ∈ E} EP(m)
π'              = π renormalised over E
```

```
LichessExpectedRegret(P) = Regret(P, π_lichess, E)
ExpertExpectedRegret(P)  = Regret(P, π_expert,  E)      # expert = local2200, else masters
KnowledgeGap(P)          = LichessExpectedRegret(P) − ExpertExpectedRegret(P)
StudyPriority(P)         = ReachProbability(P) · KnowledgeGap(P)
```

Notes and normalisations that matter:

* **A negative gap is reported, not clipped.** If the Lichess population
  conceded less than the strong-player distribution at some position, that is a
  real observation (small samples or a sharp line the 2200+ set underplays).
* `E` is the *union* of the moves that either distribution actually plays, after
  the filters of §4–§5, capped at `--max-moves-per-node` (default 6) by combined
  share. Moves excluded from `E` are not silently dropped: `eval_coverage`
  records the mass of each distribution that carries evaluations, and the metric
  is flagged `low_eval_coverage` below `MIN_EVAL_COVERAGE = 0.85`.
* **Two regret variants are stored.** `*_cp` uses `EP_cp` (available for every
  evaluation source, including the Lichess cloud). `*_wp` uses `EP_wdl` and is
  only computed where every move in `E` has engine WDL — in practice the local
  Stockfish multi-PV pass on the top candidates. The proof of concept reports the
  rank correlation between the two orderings so the "prefer WDL" preference can
  be checked rather than assumed.
* `StudyPriority` uses `ReachProbability` from the flow DP (§6), optionally
  multiplied by `KnowledgeGap` in either currency; both products are stored
  (`study_priority_cp`, `study_priority_wp`).
* **Jensen-Shannon divergence** (base 2, no smoothing, `0 log 0 = 0`) between
  `π_lichess` and `π_expert`, plus the overlap coefficient `Σ min(π_l, π_e)`, are
  stored as descriptive fields. They are deliberately *not* inputs to any
  priority: `test_divergent_but_equivalent_moves_yield_no_priority` asserts that
  two distributions as different as 0.9/0.1 versus 0.2/0.8 produce a large JS
  (≈0.40 bits) and exactly zero study value when the moves are objectively
  equivalent.

Confidence label per position: `high` when expert games ≥ 300, Lichess games ≥
5000, coverage ≥ 0.90; `medium` when ≥ 100 / ≥ 1000 / ≥ 0.80; else `low`.

---

## 3. Engine evaluation from the correct player's perspective

POV convention: **every stored evaluation is in the point of view of the side to
move of the stored FEN** (`eval.pov = 'side_to_move'`). That is what UCI and the
Lichess cloud API both report, so nothing is transformed on the way in.

Conversion happens only where a value must be expressed for another player:

* Parent multi-PV route — the position is evaluated with `multipv = 5`; the PV
  whose **first move** is the candidate move already carries the value of playing
  that move, in the mover's POV. Used directly.
* Child route — a move absent from the parent's PVs is valued by evaluating the
  *child* position and negating it:

```
cp:   EP_parent_mover = EP_cp(−cp_child)
mate: EP_parent_mover = EP_mate(−mate_child)
wdl:  EP_parent_mover = (l_child + d_child/2) / (w_child + d_child + l_child)
                       # the child's losses are the mover's wins
```

* A child position is the position **after** the move, so its side to move is the
  opponent; the WDL form above is the exact negation in expected-points terms and
  is what `metrics.flip_wdl` implements.
* Tests: `flip_ep` is involutive; `flip_wdl(wdl) == 1 − EP_wdl(wdl)` for random
  triples; mate signs negate correctly; and
  `test_method_selection_uses_the_right_field` proves the cp and WDL paths cannot
  silently mix.
* Deviations additionally store the engine's best reply in the child position
  (its PV-1 move and the score from the *opponent's* point of view after that
  reply), so "the concrete punishment" is recorded rather than asserted.

---

## 4. Moves absent from one dataset

Three distinct situations, handled separately:

1. **Move played in Lichess, absent locally (or vice versa).** Absence is not
   evidence of impossibility. The move enters the evaluated set `E` whenever it
   passes the filters, so both regrets are computed over the *same* `E`. Each
   distribution is renormalised over `E`, and the renormalisation shortfall is
   recorded per source (`expert_dropped`, `lichess_dropped`) and surfaced as the
   flag `dropped_rare_moves` when it exceeds 5%.
2. **Position absent from a source.** `coverage_state = absent` (partial source)
   or `zero` (complete source). A position with no expert distribution is not
   ranked at all; it is retained in the domain with its coverage state so the
   absence is visible and never counted as zero.
3. **Position present but a specific move never played there.** Share `0` in that
   source is a genuine zero for that source's population, and the move still
   enters `E` if the other source plays it — which is exactly the case that makes
   a knowledge gap measurable.

---

## 5. Sample-size safeguards

| guard | value | purpose |
|---|---|---|
| `MIN_EXPERT_GAMES` | 30 | expert shares below this are not treated as evidence; `masters` is used only as a fallback when `local2200` is too thin |
| `MIN_LICHESS_GAMES` | 100 | same for the Lichess distribution (harvested from a 489M-game population) |
| `MIN_MOVE_SHARE` | 0.005 | moves below this share — and below the games floor — are not evaluated |
| `MIN_MOVE_GAMES` | 5 | a move is kept if it passes *either* the share or the games floor |
| `MIN_EVAL_COVERAGE` | 0.85 | below this the metric is flagged and the position is down-weighted in reporting |
| `wilson_interval(k, n)` | 95% | attached to shares for interpretation; a 1-game 1/1 share cannot be distinguished from 60% |

Confidence labels (§2) combine dataset size with coverage. Nothing is deleted for
being small: it is labelled `low` confidence and flagged, which keeps the audit
trail honest.

---

## 6. Reach probability in a transposition graph

This was the subtlest part of the design, and the proof of concept **found a real
failure of the naive definition**: after 1.e4 d5 2.d4 e6 the board is identical
to the French 1.e4 e6 2.d4 d5, so the position's game count (141,121) exceeds the
domain entry's 2200+ games (25,451) — a "probability" of 5.5. Position counts are
not a subset of the entry population whenever a family transposes outward.

Definitions used:

* `count_ratio(P) = games(P) / games(entry)` — kept, because it is a real,
  interpretable quantity (how large this position's population is relative to the
  family entry). It may exceed 1, and that is recorded rather than hidden.
* `reach_flow(P)` — the probability that a game which played 1.e4 d5 reaches `P`,
  computed by a flow DP over the family graph:

```
flow(entry) = 1
for positions in increasing minimum ply:
    for each (child, move, games) of position:
        share = games / Σ_moves games          # empirical move share
        if child outside the domain:           leakage[position] += share
        elif child.min_ply <= position.ply:    ignored_back_mass[child] += flow·share
        else:                                  flow[child] += flow[position] · share
                                               paths[child] += paths[position]
```

  First-visit semantics match the ingestion index (a game counts once per
  position): mass arriving after a position's minimum-ply visit is recorded as
  `ignored_back_mass` instead of being added. `paths` counts distinct move-order
  paths into the position (capped at 1e15).
* Both are stored per source in `position_flow`, together with
  `n_parents_contributing`, `n_parents_total`, `leakage` and flags
  `fed_from_outside_family`, `late_transposition_arrivals`, `mass_leaves_domain`,
  `unreachable_in_flow`. Disagreement between `count_ratio` and `reach_flow` is
  itself the measurement of how much of a position's population arrives from
  outside the family — a transposition signal (research task 4).

**Provenance / reverse edges.** The complete 2200+ importer has no incoming-edge
lookup and is not modified. Instead, every `(position, move)` pair read from the
ingestion databases is replayed with python-chess and the child key is written to
`provenance` as `(child, parent, move, source, games)`. This is exact, and the
cost scales as one `push_uci` plus one key computation per edge: ~10^7 edges for
the complete index, i.e. minutes of CPU and roughly 40 bytes per row — an
affordable background batch, not an interactive operation. The proof of concept
validates the replay against the only independent child keys available: the
Lichess cache stores `target` for every move row, and the full-domain build
reproduced **7,145 of 7,145** stored keys with zero mismatches.

---

## 7. Structural fingerprints and the compression baseline

`study/structures.py` computes, per position: white and black pawn bitboards; the
central pawn configuration (white c4/d4/e4/f4, black c5/d5/e5/f5 as an 8-bit
mask); open-file mask; semi-open masks per side; pawn islands; isolated, doubled,
backward and passed pawn counts; material signature; castling state; 12
piece-square bitboards; and development count/status.

Honest limitations, documented rather than papered over:

* **backward pawns are approximate** (`backward_approximate = 1`): implemented as
  "the advance square is attacked by an enemy pawn and no friendly pawn on an
  adjacent file is level with or behind it", which is conservative but not the
  full classical notion.
* **castling**: a king standing on g1/c1/g8/c8 is read as castled; a king that
  walked there without castling is indistinguishable from the position alone.
* Structure identity is the exact `(white pawns, black pawns)` pair — colour
  aware, everything else ignored. No clustering, no hashing of approximate
  shapes.

Baseline measurements (no ML clustering), all emitted to `structures.csv` and
`summary.json`: number of exact positions in the domain; number of exact pawn
structures; the frequency distribution of structures; game coverage captured by
the top-N structures; and, per major structure, the number of distinct
development configurations (piece placement plus development status). Coverage
per structure is reported as an interval — `coverage_lower = max reach over its
positions` and `coverage_upper = min(1, Σ reach)` — because without per-game
traversal a game visiting two positions of the same structure cannot be
deduplicated exactly.

Approximate structural clustering is deliberately **not** built yet; the brief
requires the exact baseline first.

---

## 8. Structural attractor detection (research task 4)

Candidate states are exact pawn structures (and, later, positions). For each
structure S:

* `n_positions` — how many distinct boards carry the structure;
* `coverage_lower` / `coverage_upper` — the share of family games that reach it
  (interval, see §7), plus `peak_reach` for the single hottest board;
* `n_edges_in` — distinct incoming edges from provenance (how many ways in);
* `n_parents` — distinct parent positions, i.e. how many different move orders
  lead here;
* `path_count` — distinct move-order paths (DP, §6);
* `persistence` — how long games *stay* in the structure: a backwards DP over the
  family graph computing, for each position, the expected number of consecutive
  plies spent inside S (`D(P) = 1 + Σ_{children ∈ S} share · D(child)`), averaged
  over the mass that enters S;
* `n_development_signatures` — distinct piece-placement/development states inside
  S (how much variety hides behind one structure).

```
AttractorScore = coverage_lower · mean_persistence_plies · (1 + log10(1 + path_count))
```

with every factor stored separately in `structure_stat.components_json`. The
hypothesis under test: if a handful of structures combine high coverage, multiple
independent entry move orders and long persistence, then a human can learn a few
representative boards instead of a large tree. The score is a reporting device,
not a verdict; the report prints the components so the hypothesis can be judged
from the table.

---

## 9. Engine budget

Staged exactly as required (cheap filter → engine on candidates → deepen only
where the ranking could move), with the Lichess cloud doing the heavy lifting:

| stage | work | engine cost |
|---|---|---|
| 0 | domain walk + provenance replay + structural fingerprints, 29,876 positions | none (90 s wall) |
| 1 | filter to positions with ≥ 30 expert games and ≥ 100 Lichess games and ≥ 2 moves each: **172 candidates** (82 White to move) | none |
| 2 | evaluations for candidates, cloud first at depth ≥ 30, `multipv 5`, child eval only for moves missing from the parent's PVs, ≤ 6 moves per node | ~600–1,400 HTTP requests at ≥ 0.6 s spacing ≈ 6–15 min; **zero CPU** |
| 2b | local Stockfish fallback only where the cloud has no evaluation meeting the depth threshold, single thread, depth 18 | ≤ 120 searches ≈ 2–4 min, one core |
| 3 | local multi-PV (depth 18, up to 8 moves, WDL on) on the top 20 by cp-priority, to recompute regret in WDL terms and report the rank correlation | 20 searches ≈ 1 min |

**Single writer.** One analysis run at a time: `study/run_scandinavian.py` takes a
non-blocking exclusive lock on `<analysis db>.lock` and exits with status 2 if
another run holds it. Two concurrent runs would double the cloud requests and
interleave metric rows — this happened once during development and is now
prevented (`tests/test_study_singleton.py`).

Ceiling for this experiment: **~1,500 cloud requests and ~150 single-threaded
Stockfish searches**, i.e. well under half an hour of wall clock with one core
touched — small enough to run while the ingestion importers keep the machine
busy. Deeper analysis (depth 24+) is reserved for the top few candidates and for
positions where the ranking is sensitive to evaluation error.

Engine: Stockfish 19 (official `sf_19` linux x86-64 build), `Threads=1`,
`Hash=64`, `UCI_ShowWDL=true`. Cloud requests are unauthenticated, serial, with a
`User-Agent`, 404 treated as "not analysed in the cloud" and 429 as "wait and
skip" — never as a zero evaluation.

---

## 10. Opponent deviations (research task 2)

Computed only at nodes where the **opponent** (Black, in this domain) is to move;
our own decisions are the study-priority table. Per Black move at a position:

```
main set M(P)      = {most played expert move} ∪ {moves with expert share ≥ 0.10}
prob_deviation(m)  = π_expert(m)          # the population we will actually face
concession(m)      = EP_best − EP(m)      # ≥ 0, in Black's own currency
DeviationPriority(m) = prob_deviation(m) · concession(m)
class(m)           = playable      if concession ≤ 0.02 EP
                     inaccurate    if 0.02 < concession ≤ 0.10 EP
                     punishable    if concession > 0.10 EP
best_response(m)   = engine PV-1 move in the child position, with its score
                     from White's perspective (the concrete punishment)
```

The class boundaries are configuration, not truth: they are recorded next to the
raw concession so any reader can re-cut the taxonomy. Moves below a 2% share are
not reported as deviations. The four intended categories (normal/main, playable
alternative, strategically inaccurate deviation, concretely punishable mistake)
map onto `main`, `playable`, `inaccurate`, `punishable` — and no prose
explanations are generated.

---

## 11. Success criterion

Not "the pipeline ran". The question the first output must answer is whether the
top-ranked positions, deviations and structures look like things a strong human
would genuinely put on a study list. The proof of concept therefore prints, for
each of the top 20 White-to-move positions: the FEN, `reach_flow` and
`count_ratio`, expert and Lichess sample sizes, both regrets, the gap, the JS
divergence, the best engine move, evaluation coverage, confidence and flags — so
the ranking can be argued with rather than trusted.
