# Evaluation campaign: bounded, information-targeted, and the road to game-level validation

Phase deliverable. Nothing in this document is a promise about work already done:
the candidate set, the sampling design, the node budget and the trace schema exist
and are tested; **no engine run has been started**, and the PGN replay is written
but gated until the importer finishes.

Research question of this phase:

> Does one reusable structural template buy more expected playing strength per unit
> of learning burden than memorising many exact positions?

The campaign exists to make that question answerable, because the expected-value
axis currently rests on 523 cached evaluations covering 472 of 29,876 domain
positions (1.58%), and **not one board in any template sample has an evaluation**.

--------------------------------------------------------------------------------

## 1. Candidate-selection logic

Every position in the domain is scored for information value; a position enters the
sheet when it has at least one reason *and* its cached evaluation is insufficient.
Reasons are stored separately (`evaluation_candidate.reasons_json`), never merged
away; the scalar `score` is a declared convenience for ordering only.

| reason | threshold | what it captures |
|---|---|---|
| `high_reach` | flow reach ≥ 0.002 | positions many games pass through |
| `mature_template` | member of a candidate mature family | board-level value inside a template's scope |
| `behavioural_disagreement` | expert vs ordinary JS ≥ 0.10 bits **or** different top move | where ordinary play and strong play differ |
| `decision_priority` | stored study priority ≥ 0.0005 | the decision-knowledge class |
| `deviation_priority` | deviation priority ≥ 0.0005 | the exception class |
| `split_involvement` | family has an accepted split | boards where the split decides something |
| `frontier_sensitivity` | in the scope of a top-8 item by marginal value per cost | positions whose value moves the curriculum frontier |

An evaluation counts as **insufficient** when it is missing, has no WDL, has fewer
than 5 PVs, or is a non-cloud evaluation shallower than depth 20. This is why
positions with an existing Lichess cloud evaluation at depth 30-60 are not re-run
locally: cloud-first remains the rule, local tiers only fill the gaps.

Result on current data: **1,979 positions** carry at least one reason and an
insufficient evaluation; the sheet selects the top **1,200** (inside the requested
500-1,500), truncating at a score of 1.5.

Reason histogram over the selected batch:

| reason | positions |
|---|---|
| mature_template | 1,066 |
| split_involvement | 635 |
| high_reach | 230 |
| behavioural_disagreement | 123 |
| deviation_priority | 45 |
| decision_priority | 16 |
| frontier_sensitivity | 3 |

The batch is dominated by template boards and split-involved families, i.e. it is
pointed exactly at this phase's research question rather than spread evenly.

## 2. Positions per engine tier, and the promotion rule

| tier | nodes | MultiPV | positions |
|---|---|---|---|
| A | 5,000,000 | 5 | 1,115 |
| B | 25,000,000 | 5 | 60 |
| C | 100,000,000 | 5 | 25 |

Tier A is the baseline for every selected position. B and C are **not** universal:
a position is marked `promotion_watch` when it has ≥2 reasons, at least one of
template/decision/deviation/disagreement, and reach ≥ 0.002; 85 positions qualify.
Promotion itself is decided by the *measured* Tier-A result, not by the sheet:

* promote to **B** when the Tier-A ranking is threshold-adjacent — the second-best
  move is within 0.03 EP of the best, or the taught answer's loss is within 0.02 EP
  of a decision threshold (configured constants, not tuned);
* promote to **C** only when two independent moves are within 0.015 EP *and* the
  position carries ≥3 reason classes.

So the deep tiers are earned per position rather than assigned by rank, and a
position whose Tier-A result is decisive is never re-run.

## 3. Total node budget

| tier | positions | nodes each | tier total |
|---|---|---|---|
| A | 1,115 | 5,000,000 | 5,575,000,000 |
| B | 60 | 25,000,000 | 1,500,000,000 |
| C | 25 | 100,000,000 | 2,500,000,000 |
| **total** | 1,200 | | **9,575,000,000 nodes** |

## 4. Expected runtime on current hardware

Measured on this machine with the local Stockfish 19 build, single thread, Hash 64,
MultiPV 5: **505,000 nodes/second** (200k nodes in 0.40 s, 2M nodes in 3.95 s; the
engine reports its own node count, so the bookkeeping is exact).

| tier | engine time |
|---|---|
| A | 3.07 h |
| B | 0.83 h |
| C | 1.38 h |
| **total** | **5.27 h** |

Five to six hours of single-threaded engine time for the whole campaign, with the
node ceiling enforced by the runner (`run_campaign` refuses to start without
`--confirm` and stops when the next position would exceed the ceiling).

## 5. Template sample design

For each of the 8 mature templates, boards are drawn as follows:

* sort the family's boards by flow reach; split into up to 10 reach-quantile strata;
* draw inside each stratum with probability proportional to reach, so the sample is
  flow-weighted rather than uniform over boards;
* target 20-40 boards per template (30 requested; the smallest family has 18 boards
  in total and therefore yields 16);
* deterministic given the seed (1,337), so the sample is reproducible and auditable;
* persist to `template_sample(structure_id, position_key, stratum, weight)`.

Sampled sizes: `27e1bd6f` 29, `7bfcc7ad` 30, `dbb34be9` 29, `bc5997a2` 26,
`067a789a` 25, `2cf89575` 24, `546d3951` 22, `c334e682` 16.

For each sampled board the campaign stores engine top moves (MultiPV 5 with WDL),
the expert-popular moves, the ordinary-popular moves, and the template's implied
answers — four separately labelled answer sets, so the comparison cannot silently
mix them.

## 6. Exact EV formula used to value template knowledge

Per board, with `scores` the engine evaluations, `best` the best evaluated move at
that position, `q` the ordinary-population distribution and `A` the taught answers:

```
loss_pop(b)    = sum_m  q_hat(m) * max(0, EP(best) - EP(m))        q_hat = q renormalised
loss_taught(b) = sum_m  a_hat(m) * max(0, EP(best) - EP(m))        a_hat = A renormalised
gain(b)        = loss_pop(b) - loss_taught(b)
```

Both losses use the **same** baseline `best`. Measuring each against a best computed
inside its own distribution would manufacture a gain from the comparison itself; a
unit test now pins this (a taught answer unrelated to the played moves gains
exactly zero).

Flow-weighted template value over the sample, then extrapolated to the template's
scope:

```
EV_per_board(T) = sum_b w_b * gain(b) / sum_b w_b          (w_b = flow reach)
EV_scope(T)     = EV_per_board(T) * sum_{b in scope(T)} reach(b)
```

Boards without evaluations are counted and reported (`boards_missing_evaluations`),
never dropped silently — this is what makes the current zero-evaluation state
visible instead of looking like a zero effect.

Comparison "value gained per complexity unit" uses the declared complexity vector
from the curriculum layer (exact moves, boards, flexible slots, recognition
conditions, exceptions) with the scalar cost as a declared convenience only:

```
value_per_cost(kind) = sum of marginal EV over items of that kind / sum of their costs
```

computed for templates, exact decision nodes and deviations, with overlap handled by
the existing max-not-sum rule so that a template and the decisions inside it cannot
both claim the same value.

## 7. Proposed compact game-trace schema

Normalised rows with integer dictionaries, not JSON blobs:

```
trace_dict_position(ord_ PK, position_key BLOB UNIQUE)      -- 29,876 rows
trace_dict_structure(ord_ PK, structure_id TEXT UNIQUE)     --  4,065 rows
trace_dict_move(ord_ PK, uci TEXT UNIQUE)
trace_dict_source(ord_ PK, source TEXT UNIQUE)
trace_game(game_id PK, source_ord, date_ord, ratings_ord, result_ord,
           split_chrono, split_random, steps, plies)
trace_step(game_id, ply, position_ord, structure_ord, uci_ord,
           PRIMARY KEY(game_id, ply)) WITHOUT ROWID
+ indexes on step(position_ord), step(structure_ord), game(split_chrono, date_ord),
  game(split_random)
```

Property by property:

* **train/test separation by game** — a game is one `game_id`; no step can migrate
  between splits because the split lives on the game row;
* **chronological split primary** — `date_ord` (yyyymm) supports it; the boundary
  month is pushed whole into test so a single event cannot straddle the split;
* **random game-level split secondary** — a seeded SHA-256 of `game_id`;
* **exact traversal through families and templates** — `structure_ord` on every step,
  so a family or template walk is an index scan, not a re-derived fingerprint;
* **motif independence** — support can be recomputed per game, so a motif repeated
  by one game cannot inflate its support the way pooled flow counts can;
* **per-game curriculum evaluation** — `walked_games` (engine-free) and
  `curriculum_walk_with_evals` (engine-based) both iterate games and sum either
  agreement or expected loss, reporting the covered step fraction.

## 8. Projected disk cost

Measured, not guessed (`trace.measured_row_cost`): **51.1 bytes per step** and
**254 bytes per game** all-in including indexes, for this domain's real value
distributions (29,876 positions, 4,065 structures, ~26k moves, ~5.27 steps/game).

Steps per game come from the flow, not from `position_source`: `position_flow`
enter-mass sums to 5.270 domain positions per game and is self-consistent with its
own move distributions, whereas **8.2% of `position_source` rows claim more games
than their parent** (worst case 141,121 against a parent with 48), so that column is
unusable for projection.

| corpus | domain games | steps | trace size |
|---|---|---|---|
| 2200+ index (current) | 25,451 | 134,118 | ~6.5 MB |
| all-games pass (partial) | 46,163 | ~243,000 | ~11.7 MB |

The trace is therefore a few megabytes, not a storage problem: a binary blob format
would save single-digit megabytes while giving up every query listed in section 7.

## 9. Projected replay runtime

One read-only pass over `data/LumbrasGigaBase_OTB_Complete.pgn` (8.1 GB). The running
importer sustains ~363 games/second over the same corpus (9,017,000 games parsed in
about 6.9 h). The trace pass adds a dictionary lookup per move and ~5 rows per
Scandinavian game, which is negligible next to PGN parsing:

| work | estimate |
|---|---|
| 1,000,000 games | ~46 min |
| full corpus (est. 10-11M games) | ~8-10 h |
| writes produced | ~134k steps + ~25k game rows for the 2200+ subset |

It must run alone: the analysis database is single-writer, and the parse competes
for CPU with the importer, so it is scheduled after the importer reports complete.

--------------------------------------------------------------------------------

## Status and what this changes

* No engine run started; no PGN replay started. Both are gated behind explicit flags
  (`campaign.run_campaign(confirm=True, node_ceiling=...)` and
  `trace.iter_domain_games`, which nothing calls yet).
* The candidate sheet, the tier rules, the sampler, the EV formula, the trace DDL and
  the split logic are implemented and covered by 28 new tests (140 in the suite).
* The value axis is still empty for templates: `estimate_template_ev` returns
  `boards_evaluated: 0` for all 8 templates, which is precisely the gap this
  campaign fills, and it says so rather than reporting a zero effect.
* Data-quality finding worth keeping: `position_source.games` is internally
  inconsistent and must not be used for projections; the flow is used instead.
