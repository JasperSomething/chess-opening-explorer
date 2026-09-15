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

--------------------------------------------------------------------------------

# Revision 2 — remote execution, the position_source audit, and rule semantics

## 10. Remote / parallel runner design

`study/remote.py`. The 1,200 positions are independent, so execution moves to a
rented CPU machine without touching the experiment.

**Invariants copied, not re-derived.** The runner imports `campaign.NODE_TIERS`
(5M / 25M / 100M) and `campaign.MULTIPV` (5). It never redefines or rescales them.
The candidate sheet is the same sheet: jobs are planned from
`evaluation_candidate WHERE selected=1`, and the tier promotion rule is unchanged
(promotion is decided by the measured tier-A result, not by rank).

**One job, one idempotency key.** `job_id = sha1(fen|multipv|nodes_budget|engine_version)`
with `UNIQUE(fen, multipv, nodes_budget, engine_version)` on the ledger. A job that
exists in *any* state — pending, leased, done, failed — is not planned again, and the
same position at a different node budget is a different job. Depth achieved is
metadata stored on the job; nodes is the budget, the key, and the stopping condition.

**Server-free coordination, two modes.**

* *Queue mode* (default): a shared SQLite ledger. `claim_next` is a single
  conditional `UPDATE` — the job is leased only if it is pending or its lease has
  expired — and the row count decides the winner, so two workers cannot take the
  same job. Leases expire after `lease_seconds` (default 900) and
  `release_expired` returns them to pending. Killing a worker loses at most the jobs
  it currently holds.
* *Shard mode*: `export_jobs` writes JSONL, workers run offline, `import_results`
  merges back. Importing the same file twice changes nothing the second time, and
  results are also written into the existing `eval` cache so current consumers see
  them.

One coordination process per machine (one ledger, one counter, one lock); workers
within it run concurrently.

## 11. Benchmark procedure (topology selection)

`benchmark_topologies(binary, topologies, fens, nodes_per, repeat)` runs each
topology — `{'workers': 8, 'threads_per_worker': 1}` versus
`{'workers': 4, 'threads_per_worker': 2}`, plus any others — launching the workers,
searching every position at exactly `nodes_per` nodes with MultiPV 5, timing from
first start to last finish, and reporting per topology: `wall_seconds`,
`total_nodes`, `aggregate_nps = total_nodes / wall_seconds`, `mean_depth_achieved`,
and `cores` from `os.cpu_count()`. The list is sorted by `aggregate_nps` and `best`
names the winner.

Procedure for the rented machine: take `fens` from the top of the candidate sheet
(mixed tiers), run `--nodes 1000000 --fens 8` over both topologies, and pick the
winner by aggregate nodes/second — not by workers per core. Reported locally:
**505,000 nps single-thread**, which is the reference the topology has to beat.

## 12. `position_source.games` audit

Semantics, established from the writer (`study/domain.py:218-226`): the column holds
the source database's **published global population** for a position, and
`reach_prob = games / entry_games` is that source's published probability of
reaching the position. It is a global position-population quantity, **not**
family-local transition traffic. The 8.2% of links where a child claims more games
than one of its parents is consistent with that reading: a position reached by many
move orders has a large global population even when one particular parent move is
rare (worst case 141,121 against a parent with 48).

| site | use | classification | action |
|---|---|---|---|
| `domain.py:218-226` | writer: published global count and ratio | definitional (global) | unchanged; semantics documented |
| `flow.py:91-93` | `reach_prob` as `count_ratio` to flag `fed_from_outside_family` | safe: global quantity compared against the family flow, diagnostic only | unchanged, comment added |
| `run_scandinavian.py:111-118` | `lichess['games']` as a support floor, presence test | safe: global sample-size gate | unchanged |
| `run_scandinavian.py:185-196` | `games` for confidence thresholds (≥300 / ≥5000) | safe: global sample size | unchanged |
| `run_scandinavian.py:189-191` | `reach_prob` as a reach fallback in `StudyPriority` | safe but silently mixed with flow reach | **fixed**: `reach_source` now records `position_flow.reach_flow` or `published_global_ratio:<source>`, plus a flag when the fallback is used |
| `run_scandinavian.py:301-305` | per-position `mass` for the compression baseline, preferring `local2200` and falling back across sources | **unsafe**: global counts from different populations mixed as if they were transition traffic | **replaced** with `position_flow.reach_flow`; positions outside the flow are reported as unknown, not zero |
| `family.py:734-736` | availability gate + `other_games` display column | display-only | gate now uses `coverage_state` explicitly; column relabelled `other_population_kind='global_published'` |
| `run_scandinavian.py:45` | `load_sources` (report display) | display-only | unchanged |
| `trace.py:317` | deliberately avoided for projections | safe by refusal | unchanged |
| analysis scripts (ad hoc, this session) | domain-size projections | **unsafe, already corrected** | projections now come from the flow |

Net: one unsafe computational use replaced, one silent provenance gap closed, one
display label made honest, and four safe uses documented as safe. The ingestion
database was not touched.

## 13. Recognition versus prescriptive templates

`study/rules.py`. A rule is now an explicit object:

```
IF structural_family = S AND role = R [AND feature F = value] -> answer set A
```

with `support` (flow mass share where strong play's board answer is inside A),
`strict_support` (share where the board's own answer set equals A exactly),
`coverage`, `applicability` (share of the role's mass the condition selects),
`exceptions` (boards where strong play chose outside A, with their mass and the move
it chose instead), `competing` (the moves that compete with A and their mass), and
`derivation` (source, share floor, game floor, aggregation method). The two kinds:

* `prescriptive` — names one or more moves to play. **Only these earn EV.**
* `recognition_only` — orientation. Charged its learning burden; EV is `None` with a
  reason, and `None` is never rendered as zero.

Two mechanisms enforce the separation, and both were added because the code got it
wrong first:

1. In the value layer, an item with no answer set is recorded as `None`, never as
   `0.0`. A taught loss of zero means *perfect play*, so scoring recognition
   knowledge as zero granted it the entire value axis and made orientation items top
   the frontier. This is exactly the failure the distinction exists to prevent.
2. In the comparison, value is the **reduction** of the ordinary-play loss:
   `retained = Σ reach · max(0, loss_population − min_i loss_i)`, with the learner
   using the best item at a position. The earlier version maximised the taught loss,
   which inverted both the selection order and the meaning of "value retained".

A rule is only an instruction for the side to move, so families are partitioned by
role before any rule is derived.

## 14. How many templates actually admit a defensible action rule?

Top single answer per (family × side to move) over the 8 mature families, measured on
flow mass: 0.22, 0.27, 0.33, 0.34, 0.36, 0.37, 0.37, 0.37, 0.39, 0.44, 0.49, 0.51,
0.56, 0.57, 0.57, 0.59. Best two-answer set: 0.43 to 0.89.

| bar | single-move rules | ≤2-move rules (matched strictly) |
|---|---|---|
| ≥0.30 | 14 of 16 scopes, 8 families | 16 of 16 scopes |
| ≥0.40 | 7 of 16 scopes, 4 families | 16 of 16 scopes |
| ≥0.50 | 5 of 16 scopes, 3 families | 15 of 16 scopes |
| ≥0.60 | 0 of 16 scopes, 0 families | 11 of 16 scopes, 6 families |

Answer to the question as asked, with the bar declared: under a strict bar of **one
taught move covering at least 60% of the side-to-move's flow mass, no mature family
qualifies (0 of 8)**. At 50% it is **3 of 8 families**; at 40%, 4 of 8. A two-move
answer set covering 60% exists for 6 of 8 families but requires strict matching, and
the single run that survives every requirement is `2cf89575` as black with A = {d6b6},
support 1.00, applicability 0.17 — i.e. a narrow condition, not a family-wide rule.

The substantive finding: **the mature families are predominantly recognition
knowledge, not action rules.** That is precisely why the fairness fix matters — a
template-versus-exact-decision comparison would otherwise have credited templates for
moves they do not actually specify.

## 15. The six curricula, ready to compare

`curriculum.compare_curricula` produces the required table on one shared value
surface with one shared denominator (the population loss is a property of the domain,
not of how much a curriculum covers):

| curriculum | items | burden | recognition | evaluated positions | recovery |
|---|---|---|---|---|---|
| ordinary_baseline | 0 | 0 | 0.000 | 0 | 0.000 |
| exact_decisions_only | 25 | 50 | 1.000 | 13 | 0.447 |
| prescriptive_templates_only | 16 | 268 | 0.429 | 0 | — (no evaluated board in scope yet) |
| deviations_only | 20 | 54 | 0.007 | 5 | 0.000 |
| hybrid_templates_plus_exceptions | 36 | 322 | 0.436 | 5 | 0.000 |
| engine_informed_ceiling | 80 | 493 | 1.000 | 57 | 0.447 |

Read with the eval gap in mind: the ceiling currently equals the decisions-only
curriculum because nothing else has an evaluated board in scope. Two further
observations that are findings rather than artefacts: deviation items sit on
very low-reach positions (≤0.22% of games each, 13 of 20 non-zero, total recognition
0.7%), so the exception class cannot carry much EV as currently scoped; and the
prescriptive-template arm has 16 items and a burden of 268 against 25 items and a
burden of 50 for the exact-decision arm, which is the cost side of the comparison
this phase is meant to settle.
